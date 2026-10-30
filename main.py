"""社交图谱：被动收听群聊/私聊，攒出对每个人的印象与彼此的关系，
再在 LLM 请求前把「认识这个人」的那一小段注入进去。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.star_tools import StarTools

from .store import SocialStore

PLUGIN_NAME = "astrbot_plugin_social_graph"

# 桥接插件（astrbot_plugin_denia_album）接管的转发消息，不记录下来
BRIDGE_PREFIX = re.compile(
    r"^\s*(?:转告大肥鱼|告诉大肥鱼|和大肥鱼说|跟大肥鱼说|给大肥鱼说|找大肥鱼|大肥鱼)\s*[，,、：:。\s]*"
)

EXTRACT_SYSTEM = """你是一个社交记忆整理器。你会读到一段聊天记录，然后把「值得长期记住的社交信息」写成结构化 JSON。

规则：
1. 只写聊天记录里**明确支持**的内容。宁缺勿滥：没信息的人不要出现。
2. 印象（impressions）是「对这个人的认识」：身份/称呼、说话风格、关注什么、和机器人（达妮娅）的关系与态度。每条 40-160 字，用第二人称之外的客观口吻（例如「是个喜欢…的男生，说话直接」）。
3. 关系（relations）只记录聊天里**看得出来**的人际关系，例如「a 和 b 是同学」「a 在追 b」「a 常怼 b」。relation 用 2-8 个字的中文短语。confidence 0-1，模糊的给 0.4-0.6。
4. 已有印象会在开头给出。请把它和新的聊天记录**合并**成一段更完整的印象，不要简单堆叠，不要丢掉旧信息。总长度不超过 300 字。
5. 输出**只包含** JSON，不要解释，不要代码块围栏。格式：
{"impressions":[{"uid":"123","name":"昵称","text":"合并后的印象"}],
 "relations":[{"a_uid":"123","b_uid":"456","relation":"同学","confidence":0.8,"evidence":"…"}]}
6. 没有可写的内容就输出 {"impressions":[],"relations":[]}。"""


@register(PLUGIN_NAME, "local", "社交图谱：记住每个人的印象与彼此的关系", "0.1.0")
class SocialGraphPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        try:
            data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            data_dir = Path("data/plugin_data/social_graph")
        self.store = SocialStore(data_dir / "social.db")
        self._task: asyncio.Task | None = None
        self._stop = False
        self._extract_lock = asyncio.Lock()
        self._last_extract = 0.0
        self._last_prune = time.time()
        self._interject_state: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self):
        self._register_page_web_apis()
        self._task = asyncio.create_task(self._extract_loop())
        try:
            removed = self.store.prune_events(int(self.config.get("keep_days", 30)))
            if removed:
                logger.info(f"[social_graph] 清理过期原始消息 {removed} 条")
        except Exception as e:
            logger.warning(f"[social_graph] 清理旧消息失败: {e}")
        n = self.store.unprocessed_count()
        logger.info(f"[social_graph] 已启动，待抽取事件 {n} 条")

    async def terminate(self):
        self._stop = True
        if self._task:
            self._task.cancel()
        self.store.close()

    # ------------------------------------------------------------------
    # 收听：所有消息都过一遍，只记录，不回复、不打断
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def observe(self, event: AstrMessageEvent):
        try:
            text = (event.message_str or "").strip()
            sender = str(event.get_sender_id() or "")
            self_id = str(event.get_self_id() or "")
            if not sender or sender == self_id:
                return
            if BRIDGE_PREFIX.match(text):
                return
            if text.startswith("/") or text.startswith("／"):
                return
            group_id = str(event.get_group_id() or "")
            chat_kind = "group" if group_id else "private"
            chat_id = group_id or sender
            name = event.get_sender_name() or sender

            reply_to = ""
            extra = []
            for comp in getattr(event.message_obj, "message", []) or []:
                ctype = getattr(comp, "type", "")
                ctype = getattr(ctype, "value", ctype)
                if ctype == "reply":
                    rid = str(getattr(comp, "sender_id", "") or "")
                    rname = getattr(comp, "sender_nickname", "") or ""
                    rtext = (getattr(comp, "message_str", "") or "")[:80]
                    if rid:
                        reply_to = rid
                        extra.append(f"[引用 {rname}({rid}): {rtext}]")
                elif ctype == "at":
                    qq = str(getattr(comp, "qq", "") or "")
                    if qq and qq != "all":
                        extra.append(f"[@{qq}]")
                elif ctype == "image":
                    extra.append("[图片]")
            if not text and not extra:
                return
            body = " ".join([text] + extra).strip()

            self.store.touch_person(sender, name)
            self.store.add_event(
                ts=int(time.time()),
                umo=event.unified_msg_origin,
                chat_kind=chat_kind,
                chat_id=chat_id,
                speaker_uid=sender,
                speaker_name=name,
                text=body[:800],
                reply_to=reply_to,
            )
            if chat_kind == "group":
                await self._maybe_interject(event, text, group_id, name, self_id)
        except Exception as e:  # 收听永不抛错，避免影响主链
            logger.warning(f"[social_graph] 记录消息失败: {e}")

    # ------------------------------------------------------------------
    # 群聊插话：偶尔自己接一句，不 @ 也接
    # ------------------------------------------------------------------
    INTERJECT_SYSTEM = (
        "你在一个 QQ 群里，群友正在聊天。你是达妮娅，一个说话慵懒、随便、带点玩笑的女生。"
        "你的任务只是判断：这群人刚才聊的，你有没有想接一句。"
        "想接就只输出你要说的那一句话（不超过 20 个字，口语，不要表情符号堆砌，不要 @ 人）；"
        "不想接、听不懂、或者话题轮不到你，就只输出 SKIP 四个字母。"
        "大多数时候你都应该输出 SKIP——只在真的有意思、或者有人提到你在意的事时才接。"
    )

    async def _maybe_interject(self, event, text: str, group_id: str, name: str, self_id: str):
        if not self.config.get("interject_enabled", True):
            return
        if len(text) < 2:
            return
        # 有人 @ 她，交给主链正经回复，这里不掺和
        for comp in getattr(event.message_obj, "message", []) or []:
            ctype = getattr(comp, "type", "")
            ctype = getattr(ctype, "value", ctype)
            if ctype == "at" and str(getattr(comp, "qq", "") or "") == self_id:
                return

        now = time.time()
        st = self._interject_state.setdefault(
            group_id, {"day": "", "count": 0, "last": 0.0}
        )
        day = time.strftime("%Y-%m-%d")
        if st["day"] != day:
            st["day"] = day
            st["count"] = 0
        limit = int(self.config.get("interject_daily_limit", 8))
        if st["count"] >= limit:
            return
        cooldown = int(self.config.get("interject_cooldown_min", 25)) * 60
        if now - float(st["last"]) < cooldown:
            return
        prob = float(self.config.get("interject_probability", 10))
        if random.random() * 100.0 >= prob:
            return

        provider = self._interject_provider()
        if provider is None:
            return

        rows = self.store.recent_events(limit=10, chat_id=group_id)
        lines = []
        for r in rows:
            who = r.get("speaker_name") or r.get("speaker_uid") or "某人"
            lines.append(f"{who}: {r.get('text') or ''}")
        transcript = "\n".join(lines[-10:])
        prompt = f"最近的群聊记录：\n{transcript}\n\n刚说完的是 {name}。你要接一句吗？"

        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt=self.INTERJECT_SYSTEM,
                session_id=f"interject:{group_id}",
            )
            reply = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:
            logger.warning(f"[social_graph] 插话判断失败: {e}")
            return

        reply = reply.strip().strip("「」\"'")
        if not reply or reply.upper().startswith("SKIP") or len(reply) > 40:
            return

        try:
            ok = await self.context.send_message(
                event.unified_msg_origin, MessageChain().message(reply)
            )
        except Exception as e:
            logger.warning(f"[social_graph] 群聊插话发送失败: {e}")
            return
        if ok:
            st["count"] += 1
            st["last"] = now
            logger.info(f"[social_graph] 群聊插话 group={group_id}: {reply}")

    def _interject_provider(self):
        try:
            pid = self.config.get("interject_provider_id", "") or self.config.get(
                "extract_provider_id", "deepseek/deepseek-flash"
            )
            p = self.context.get_provider_by_id(pid)
            if p:
                return p
        except Exception:
            pass
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 抽取：攒够了或闲下来了，批量调一次模型
    # ------------------------------------------------------------------
    async def _extract_loop(self):
        await asyncio.sleep(20)
        while not self._stop:
            try:
                await asyncio.sleep(60)
                batch_min = int(self.config.get("batch_min", 40))
                idle_secs = int(self.config.get("idle_secs", 900))
                min_gap = int(self.config.get("min_gap_secs", 300))
                n = self.store.unprocessed_count()
                if n <= 0:
                    continue
                if time.time() - self._last_extract < min_gap:
                    continue
                idle = time.time() - self.store.last_event_ts()
                if n < batch_min and idle < idle_secs:
                    continue
                await self._extract_once()
                if time.time() - self._last_prune > 86400:
                    self._last_prune = time.time()
                    try:
                        removed = self.store.prune_events(int(self.config.get("keep_days", 30)))
                        if removed:
                            logger.info(f"[social_graph] 清理过期原始消息 {removed} 条")
                    except Exception as e:
                        logger.warning(f"[social_graph] 清理旧消息失败: {e}")
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"[social_graph] 抽取循环异常: {e}")

    async def _extract_once(self):
        async with self._extract_lock:
            batch = self.store.take_unprocessed(200)
            if not batch:
                return
            self._last_extract = time.time()
            provider = self._pick_provider()
            if provider is None:
                logger.warning("[social_graph] 没有可用 provider，跳过本轮抽取")
                return

            names: dict[str, str] = {}
            for ev in batch:
                names[ev["speaker_uid"]] = ev["speaker_name"] or ev["speaker_uid"]
            known = []
            for uid, nm in names.items():
                p = self.store.get_person(uid)
                if p and p.get("impression"):
                    known.append(f"- {nm}({uid}) 既有印象：{p['impression']}")
            existing = "\n".join(known) if known else "（暂无）"

            lines = []
            for ev in batch:
                chat = "群" + ev["chat_id"] if ev["chat_kind"] == "group" else "私聊"
                line = f"[{chat}] {ev['speaker_name']}({ev['speaker_uid']}): {ev['text']}"
                lines.append(line)
            transcript = "\n".join(lines[-200:])

            prompt = (
                f"已知印象：\n{existing}\n\n聊天记录：\n{transcript}\n\n"
                "请按规则输出 JSON。"
            )
            try:
                resp = await provider.text_chat(
                    prompt=prompt,
                    system_prompt=EXTRACT_SYSTEM,
                    contexts=[],
                )
                raw = getattr(resp, "completion_text", "") or ""
            except Exception as e:
                logger.warning(f"[social_graph] 抽取调用失败: {e}")
                return

            data = self._parse_json(raw)
            if data is None:
                logger.warning(f"[social_graph] 抽取结果不是 JSON，本轮跳过: {raw[:200]}")
                return

            n_imp = 0
            for item in data.get("impressions", []) or []:
                uid = str(item.get("uid", "")).strip()
                text = str(item.get("text", "")).strip()
                if not uid or not text:
                    continue
                if not self.store.get_person(uid):
                    self.store.touch_person(uid, str(item.get("name", "") or uid))
                self.store.set_impression(uid, text[:300])
                n_imp += 1

            n_rel = 0
            for item in data.get("relations", []) or []:
                a = str(item.get("a_uid", "")).strip()
                b = str(item.get("b_uid", "")).strip()
                rel = str(item.get("relation", "")).strip()
                if not a or not b or not rel:
                    continue
                try:
                    conf = float(item.get("confidence", 0.5))
                except Exception:
                    conf = 0.5
                self.store.upsert_relation(a, b, rel, conf, str(item.get("evidence", "")))
                n_rel += 1

            self.store.mark_processed([ev["id"] for ev in batch])
            logger.info(
                f"[social_graph] 抽取完成: 事件 {len(batch)} 条, 印象 {n_imp} 条, 关系 {n_rel} 条"
            )

    def _pick_provider(self):
        try:
            pid = self.config.get("extract_provider_id", "deepseek/deepseek-flash")
            if pid:
                p = self.context.get_provider_by_id(pid)
                if p:
                    return p
        except Exception:
            pass
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    @staticmethod
    def _parse_json(raw: str):
        raw = (raw or "").strip()
        if not raw:
            return None
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        try:
            return json.loads(raw)
        except Exception:
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except Exception:
                return None
        return None

    # ------------------------------------------------------------------
    # 注入：把「认识这个人」的那一小段贴进 system prompt
    # ------------------------------------------------------------------
    # 必须排在 private_companion 的 -260000 之后：它会用「注入计划」重写 system_prompt，
    # 排早了写进去的内容会被整段覆盖掉。
    @filter.on_llm_request(priority=-300000)
    async def inject_social_context(self, event: AstrMessageEvent, req):
        try:
            if not bool(self.config.get("inject_enabled", True)):
                return
            uid = str(event.get_sender_id() or "")
            if not uid:
                return
            blocks = []
            person = self.store.get_person(uid)
            if person and person.get("impression"):
                blocks.append(f"- {person['nickname'] or uid}({uid})：{person['impression']}")

            # 私聊里提到别人名字时，把那个人的印象也带上
            msg = event.message_str or ""
            mentioned = []
            if msg:
                for p in self.store.find_people("", limit=200):
                    nm = (p.get("nickname") or "").strip()
                    if len(nm) >= 2 and nm in msg and p["uid"] != uid and p.get("impression"):
                        mentioned.append(p)
                    if len(mentioned) >= 2:
                        break
                for p in mentioned:
                    blocks.append(f"- {p['nickname']}({p['uid']})：{p['impression']}")

            # 提到某个人时，把他最近说过的话也带上，这样能答「他今天说了什么」
            if mentioned:
                hours_ev = int(self.config.get("recent_hours", 6))
                since_ev = int(time.time()) - hours_ev * 3600
                for p in mentioned:
                    evs = self.store.recent_events_by_speaker(
                        p["uid"], since_ev, limit=6
                    )
                    if not evs:
                        continue
                    lines = []
                    for ev in evs:
                        where = (
                            "群" + ev["chat_id"] if ev["chat_kind"] == "group" else "私聊"
                        )
                        t = time.strftime("%m-%d %H:%M", time.localtime(ev["ts"]))
                        lines.append(f"- [{where} {t}] {ev['text'][:80]}")
                    blocks.append(
                        f"{p['nickname']}最近说过的话：\n" + "\n".join(lines)
                    )

            rels = self.store.relations_of(uid, min_conf=0.5, limit=5)
            rel_lines = []
            for r in rels:
                other = r["b_uid"] if r["a_uid"] == uid else r["a_uid"]
                op = self.store.get_person(other)
                oname = (op["nickname"] if op else "") or other
                rel_lines.append(f"- 你和 {oname}：{r['relation']}")
            if rel_lines:
                blocks.append("关系：" + "；".join(x.lstrip("- ") for x in rel_lines))

            # 「刚在群里说了什么」：私聊时只带群里的事，不带别人私聊的内容
            if not str(event.get_group_id() or ""):
                hours = int(self.config.get("recent_hours", 6))
                recents = self.store.recent_other_chat_events(
                    uid, int(time.time()) - hours * 3600, limit=8, only_group=True
                )
                if recents:
                    lines = []
                    for ev in recents:
                        where = "群" + ev["chat_id"] if ev["chat_kind"] == "group" else "私聊"
                        t = time.strftime("%H:%M", time.localtime(ev["ts"]))
                        lines.append(f"- [{where} {t}] {ev['speaker_name']}：{ev['text'][:70]}")
                    blocks.append("最近别的会话里发生的事：\n" + "\n".join(lines))

            if not blocks:
                return
            block = (
                "\n\n【你本来就知道的事】下面这些是你已经知道的人和事，不是猜测，"
                "就像你亲身经历过一样。被问到相关的人或事时，直接用这里的内容回答，"
                "可以说「嗯，他啊……」「听过」这种话；**不要说「我不记得」「没有记忆」**。"
                "自然一点，别整段复述：\n"
                + "\n".join(blocks)
            )
            req.system_prompt = (req.system_prompt or "") + block
            # 只写进 system prompt 时她常常忽略（前面 1.2 万字人设会盖过去），
            # 所以在当前这轮的用户消息后面再贴一份更短的「事实清单」。
            try:
                facts = "\n".join(blocks)
                turn = (
                    "\n\n<你记得的事>下面这些是你确实知道的，回答时直接用，"
                    "不要说你不知道：\n" + facts + "\n</你记得的事>"
                )
                req.prompt = (req.prompt or "") + turn
            except Exception:
                pass
            try:
                dump = Path(
                    StarTools.get_data_dir(PLUGIN_NAME)
                ) / "last_inject.txt"
                dump.write_text(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} uid={uid}\n{block}",
                    encoding="utf-8",
                )
            except Exception:
                pass
            logger.info(
                f"[social_graph] 注入记忆: uid={uid} 块数={len(blocks)} 字数={len(block)}"
            )
        except Exception as e:
            logger.warning(f"[social_graph] 注入失败: {e}")

    # ------------------------------------------------------------------
    # WebUI 页面接口
    # ------------------------------------------------------------------
    def _register_page_web_apis(self):
        routes = (
            ("page/overview", self.page_overview, ["GET"], "social graph overview"),
            ("page/person", self.page_person, ["GET"], "social graph person detail"),
            ("page/relations", self.page_relations, ["GET"], "social graph relations"),
            ("page/save_person", self.page_save_person, ["POST"], "save person"),
            ("page/delete_person", self.page_delete_person, ["POST"], "delete person"),
            ("page/save_relation", self.page_save_relation, ["POST"], "save relation"),
            ("page/delete_relation", self.page_delete_relation, ["POST"], "delete relation"),
            ("page/feed", self.page_feed, ["GET"], "recent messages"),
            ("page/get_config", self.page_get_config, ["GET"], "get config"),
            ("page/save_config", self.page_save_config, ["POST"], "save config"),
            ("page/extract", self.page_extract, ["POST"], "run extraction"),
        )
        for endpoint, handler, methods, desc in routes:
            try:
                self.context.register_web_api(
                    f"/{PLUGIN_NAME}/{endpoint}", handler, methods, desc
                )
            except Exception as e:
                logger.warning(f"[social_graph] 注册接口 {endpoint} 失败: {e}")

    def _data_root(self) -> Path:
        """data/plugin_data 的绝对路径（不依赖进程工作目录）。"""
        try:
            return Path(__file__).resolve().parents[2] / "plugin_data"
        except Exception:
            return Path(StarTools.get_data_dir(PLUGIN_NAME)).parent

    def _cfg_path(self) -> Path:
        return self._data_root().parent / "config" / f"{PLUGIN_NAME}_config.json"

    def _persist_config(self, patch: dict) -> dict:
        cfg = dict(self.config or {})
        cfg.update(patch)
        self.config = cfg
        path = self._cfg_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[social_graph] 保存配置失败: {e}")
        return cfg

    async def _page_query(self) -> dict:
        try:
            from quart import request as _req

            return {str(k): v for k, v in (_req.args or {}).items()}
        except Exception:
            return {}

    async def _page_body(self) -> dict:
        try:
            from quart import request as _req

            data = await _req.get_json(silent=True)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def _page_reply(self, payload):
        try:
            from quart import jsonify as _json

            return _json(payload)
        except Exception:
            return payload

    def _affection_map(self) -> dict[str, float]:
        """读好感度插件的数据（只读展示，写入权仍归它自己）。"""
        out: dict[str, float] = {}
        try:
            root = self._data_root() / "astrbot_plugin_affection"
            if not root.is_dir():
                return out
            for sub in root.iterdir():
                if not sub.is_dir():
                    continue
                f = sub / "user_data.json"
                if not f.is_file():
                    continue
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                for uid, row in data.items():
                    if isinstance(row, dict) and "affection" in row:
                        try:
                            out[str(uid)] = round(float(row["affection"]), 1)
                        except Exception:
                            continue
        except Exception as e:
            logger.debug(f"[social_graph] 读好感度失败: {e}")
        return out

    async def page_overview(self):
        keyword = (await self._page_query()).get("q", "")

        def work():
            people = self.store.list_people(keyword, limit=300)
            aff = self._affection_map()
            for p in people:
                p["affection"] = aff.get(str(p.get("uid")))
            return {
                "ok": True,
                "stats": self.store.stats(),
                "people": people,
                "config": self._public_config(),
            }

        try:
            return await self._page_reply(await asyncio.to_thread(work))
        except Exception as e:
            return await self._page_reply({"ok": False, "error": str(e)})

    async def page_relations(self):
        def work():
            rows = self.store.all_relations_raw(limit=300)
            out = []
            for r in rows:
                a = self.store.get_person(r["a_uid"])
                b = self.store.get_person(r["b_uid"])
                out.append(
                    {
                        **r,
                        "a_name": (a["nickname"] if a else "") or r["a_uid"],
                        "b_name": (b["nickname"] if b else "") or r["b_uid"],
                    }
                )
            return {"ok": True, "relations": out}

        try:
            return await self._page_reply(await asyncio.to_thread(work))
        except Exception as e:
            return await self._page_reply({"ok": False, "error": str(e)})

    async def page_person(self):
        uid = str((await self._page_query()).get("uid", "")).strip()

        def work():
            person = self.store.get_person(uid)
            if not person:
                return {"ok": False, "error": "没有这个人"}
            rels = []
            for r in self.store.relations_of(uid, min_conf=0.0, limit=50):
                other = r["b_uid"] if r["a_uid"] == uid else r["a_uid"]
                op = self.store.get_person(other)
                rels.append(
                    {
                        **r,
                        "a_name": (op["nickname"] if op and r["a_uid"] != uid else person["nickname"])
                        or r["a_uid"],
                        "b_name": (op["nickname"] if op and r["b_uid"] != uid else person["nickname"])
                        or r["b_uid"],
                    }
                )
            return {
                "ok": True,
                "person": {**person, "affection": self._affection_map().get(uid)},
                "relations": rels,
                "events": self.store.events_of_speaker(uid, limit=40),
            }

        try:
            return await self._page_reply(await asyncio.to_thread(work))
        except Exception as e:
            return await self._page_reply({"ok": False, "error": str(e)})

    async def page_save_person(self):
        body = await self._page_body()
        uid = str(body.get("uid", "")).strip()
        nickname = body.get("nickname")
        impression = body.get("impression")

        def work():
            ok = self.store.update_person(
                uid,
                None if nickname is None else str(nickname),
                None if impression is None else str(impression),
            )
            return {"ok": bool(ok), "person": self.store.get_person(uid)}

        try:
            return await self._page_reply(await asyncio.to_thread(work))
        except Exception as e:
            return await self._page_reply({"ok": False, "error": str(e)})

    async def page_delete_person(self):
        body = await self._page_body()
        uid = str(body.get("uid", "")).strip()
        drop_events = bool(body.get("drop_events", True))
        try:
            n = await asyncio.to_thread(self.store.delete_person, uid, drop_events)
            logger.info(f"[social_graph] 页面删除人物 uid={uid} 连带消息 {n} 条")
            return await self._page_reply({"ok": True, "removed_events": n})
        except Exception as e:
            return await self._page_reply({"ok": False, "error": str(e)})

    async def page_save_relation(self):
        body = await self._page_body()
        try:
            a = str(body.get("a_uid", "")).strip()
            b = str(body.get("b_uid", "")).strip()
            rel = str(body.get("relation", "")).strip()
            conf = float(body.get("confidence", 0.6) or 0.6)
            await asyncio.to_thread(
                self.store.upsert_relation, a, b, rel, conf, str(body.get("evidence", ""))
            )
            return await self._page_reply({"ok": True})
        except Exception as e:
            return await self._page_reply({"ok": False, "error": str(e)})

    async def page_delete_relation(self):
        body = await self._page_body()
        try:
            n = await asyncio.to_thread(
                self.store.delete_relation,
                str(body.get("a_uid", "")),
                str(body.get("b_uid", "")),
                str(body.get("relation", "")),
            )
            return await self._page_reply({"ok": True, "removed": n})
        except Exception as e:
            return await self._page_reply({"ok": False, "error": str(e)})

    async def page_feed(self):
        q = await self._page_query()
        try:
            limit = int(q.get("limit", 60) or 60)
        except Exception:
            limit = 60
        chat_id = str(q.get("chat_id", "") or "")
        try:
            rows = await asyncio.to_thread(self.store.recent_feed, limit, chat_id)
            return await self._page_reply({"ok": True, "events": rows})
        except Exception as e:
            return await self._page_reply({"ok": False, "error": str(e)})

    async def page_get_config(self):
        return await self._page_reply({"ok": True, "config": self._public_config()})

    async def page_save_config(self):
        body = await self._page_body()
        allowed = {
            "batch_min": int,
            "idle_secs": int,
            "min_gap_secs": int,
            "recent_hours": int,
            "keep_days": int,
            "inject_enabled": bool,
            "interject_enabled": bool,
            "interject_probability": float,
            "interject_daily_limit": int,
            "interject_cooldown_min": int,
            "extract_provider_id": str,
            "interject_provider_id": str,
        }
        patch = {}
        for key, cast in allowed.items():
            if key not in body:
                continue
            value = body[key]
            try:
                if cast is bool:
                    patch[key] = bool(value) if not isinstance(value, str) else value.lower() in ("1", "true", "yes", "on")
                elif cast is int:
                    patch[key] = int(value)
                elif cast is float:
                    patch[key] = float(value)
                else:
                    patch[key] = str(value)
            except Exception:
                continue
        try:
            cfg = await asyncio.to_thread(self._persist_config, patch)
            return await self._page_reply({"ok": True, "config": self._public_config(cfg)})
        except Exception as e:
            return await self._page_reply({"ok": False, "error": str(e)})

    def _public_config(self, cfg=None) -> dict:
        cfg = cfg or self.config or {}
        return {
            "batch_min": cfg.get("batch_min", 40),
            "idle_secs": cfg.get("idle_secs", 900),
            "min_gap_secs": cfg.get("min_gap_secs", 300),
            "recent_hours": cfg.get("recent_hours", 6),
            "keep_days": cfg.get("keep_days", 30),
            "inject_enabled": bool(cfg.get("inject_enabled", True)),
            "interject_enabled": bool(cfg.get("interject_enabled", True)),
            "interject_probability": cfg.get("interject_probability", 10),
            "interject_daily_limit": cfg.get("interject_daily_limit", 8),
            "interject_cooldown_min": cfg.get("interject_cooldown_min", 25),
            "extract_provider_id": cfg.get("extract_provider_id", "deepseek/deepseek-flash"),
            "interject_provider_id": cfg.get("interject_provider_id", ""),
        }

    async def page_extract(self):
        try:
            n = self.store.unprocessed_count()
            if n == 0:
                return await self._page_reply({"ok": True, "message": "没有待整理的消息", "rounds": 0})
            rounds = 0
            while self.store.unprocessed_count() > 0 and rounds < 6:
                await self._extract_once()
                rounds += 1
            return await self._page_reply(
                {
                    "ok": True,
                    "message": f"整理完成，共 {rounds} 轮",
                    "rounds": rounds,
                    "stats": self.store.stats(),
                }
            )
        except Exception as e:
            return await self._page_reply({"ok": False, "error": str(e)})

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------
    @filter.command("画像")
    async def cmd_profile(self, event: AstrMessageEvent):
        """看印象：/画像 [昵称或QQ号]"""
        arg = (event.message_str or "").replace("/画像", "").replace("画像", "", 1).strip()
        if not arg:
            people = self.store.find_people("", limit=15)
            if not people:
                yield event.plain_result("还没有记下任何人。先在群里或私聊里聊几句吧。")
                return
            lines = ["我记着这些人："]
            for p in people:
                imp = (p.get("impression") or "").strip()
                mark = "✔" if imp else "…"
                lines.append(f"{mark} {p['nickname'] or p['uid']}({p['uid']})  {imp[:60]}")
            lines.append("\n用 /画像 昵称 看详细。")
            yield event.plain_result("\n".join(lines))
            return

        people = self.store.find_people(arg, limit=3)
        if not people:
            yield event.plain_result(f"没找到「{arg}」。")
            return
        p = people[0]
        lines = [f"{p['nickname'] or p['uid']}（{p['uid']}）"]
        lines.append(f"消息数：{p['msg_count']}；印象更新时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(p['impression_at'] or 0))}")
        lines.append("印象：" + ((p.get("impression") or "（还没有）").strip()))
        rels = self.store.relations_of(p["uid"], min_conf=0.3, limit=8)
        if rels:
            lines.append("关系：")
            for r in rels:
                other = r["b_uid"] if r["a_uid"] == p["uid"] else r["a_uid"]
                op = self.store.get_person(other)
                oname = (op["nickname"] if op else "") or other
                lines.append(f"  · {oname}：{r['relation']}（{r['confidence']:.2f}）")
        yield event.plain_result("\n".join(lines))

    @filter.command("整理")
    async def cmd_extract(self, event: AstrMessageEvent):
        """立刻把还没处理的消息整理成印象：/整理"""
        n = self.store.unprocessed_count()
        if n == 0:
            yield event.plain_result("没有待整理的消息。")
            return
        yield event.plain_result(f"开始整理 {n} 条消息…")
        rounds = 0
        while self.store.unprocessed_count() > 0 and rounds < 6:
            await self._extract_once()
            rounds += 1
        yield event.plain_result(
            f"整理完了（{rounds} 轮）。用 /画像 看结果，/关系 看关系网。"
        )

    @filter.command("关系")
    async def cmd_relations(self, event: AstrMessageEvent):
        """看关系网：/关系"""
        rels = self.store.all_relations(min_conf=0.3, limit=30)
        if not rels:
            yield event.plain_result("还没推断出什么关系。")
            return
        lines = ["我看到的关系："]
        for r in rels:
            a = self.store.get_person(r["a_uid"])
            b = self.store.get_person(r["b_uid"])
            an = (a["nickname"] if a else "") or r["a_uid"]
            bn = (b["nickname"] if b else "") or r["b_uid"]
            lines.append(f"· {an} — {bn}：{r['relation']}（{r['confidence']:.2f}）")
        yield event.plain_result("\n".join(lines))
