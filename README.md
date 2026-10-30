# 记忆中心（astrbot_plugin_social_graph）

> 让机器人**记住每个和它聊过天的人**：这个人是谁、说话什么风格、在意什么、和别人是什么关系。
> 平时它只静静听着；聊天时把「认识这个人」的那一小段塞进上下文；你还能在 AstrBot 面板里**直接查看和修改**这些记忆。

一个 AstrBot 插件，零外部依赖，记忆存在本地 SQLite 里。

---

## 它解决什么问题

私聊里聊得好好的，一进群就「你是谁」；群里某个人天天来，一周后还是陌生人；
有人问「群里那个谁最近怎么样」，机器人只能回「我不太清楚」。
—— 因为它们没有「记住人」的地方。

这个插件把聊天现场变成一张**长期的人物关系网**：

- 谁说过什么（原始消息，默认留 30 天）
- 每个人是什么样的人（一句 40~300 字的印象，会随聊天不断合并更新）
- 谁和谁是什么关系（同学 / 群友 / 在追谁 / 常互怼，带可信度与证据）
- 私聊里提到某个群里的人时，能顺着名字把人捞出来，连他最近说过的话一起带上

---

## 功能

### 被动收听，不打扰

默认记录**所有**会话（私聊 + 群聊）里的消息，只写库、不回话、不抢主链。
跳过机器人自己、跳过 `/` 开头的命令、跳过别的插件接管的转发消息。

### 批量抽取，省 token

不是每条消息都调模型。攒够 `batch_min` 条（默认 40）或闲置 `idle_secs` 秒（默认 900）后，
把这一批连同**已有的旧印象**一起丢给一个便宜的小模型，让它输出结构化 JSON：

```json
{
  "impressions": [{"uid": "123", "name": "小林", "text": "是个…"}],
  "relations": [{"a_uid": "123", "b_uid": "456", "relation": "同学", "confidence": 0.8, "evidence": "…"}]
}
```

模型会把新旧印象**合并**成一段，而不是简单堆叠；两次抽取之间至少隔 `min_gap_secs` 秒。

### 聊天时注入

在 LLM 请求前把「你本来就知道的事」贴进上下文，分两处：

1. `system_prompt` 末尾 —— 完整的印象、关系、最近言论
2. 当前轮用户消息末尾的 `<你记得的事>…</你记得的事>` —— 一份更短的清单

**两处都要贴**是有原因的：只贴 system prompt 时，一万多字的人设会把它盖过去，
模型（尤其是带慵懒人设的）会直接说「我不记得」。贴进当前轮消息，它才会真的用。

### 群聊插话（可选）

群里没人 @ 它时，也有机会自己接一句：按 `interject_probability` 概率进入判断，
模型只允许输出一句话或 `SKIP`，受每日上限与冷却时间约束。

### WebUI 面板

面板 → 插件 → **记忆中心**：

| 标签页 | 内容 |
| --- | --- |
| 人物 | 人物列表（昵称、好感度、消息数）＋ 单项详情：改昵称、改印象、删人、加/删关系、看他最近说过的话 |
| 关系网 | 全部关系表格（A — 关系 → B、可信度、证据），可逐条删除 |
| 消息流 | 最近 120 条原始消息，可按群号过滤，标出「未整理」 |
| 设置 | 整理阈值、保留天数、注入开关、插话参数，保存即写回配置文件 |

顶部「立刻整理」＝ 聊天里的 `/整理`。
好感度（♥）读自好感度插件，**只读展示**——它的数据在内存里缓存，外部改文件会被覆盖。

### 聊天命令

| 命令 | 作用 |
| --- | --- |
| `/画像` | 列出记着的所有人（各带一句印象摘要） |
| `/画像 昵称或QQ` | 看某个人的详细印象与关系 |
| `/关系` | 看关系网 |
| `/整理` | 立刻把攒着的消息整理成印象（最多 6 轮） |

---

## 安装

1. 把整个目录放到 `AstrBot/data/plugins/astrbot_plugin_social_graph/`
2. 重启 AstrBot（或在面板里重载插件）
3. 面板 → 插件 → 记忆中心，确认页面出现；数据在 `data/plugin_data/astrbot_plugin_social_graph/social.db`
4. 想立刻看到效果：发几句消息 → `/整理` → `/画像`

**依赖**：无。只用 Python 标准库（`sqlite3`、`json`、`asyncio`）与 AstrBot 自带 API。

---

## 配置

面板里改，或直接改 `data/config/astrbot_plugin_social_graph_config.json`（改文件需重启）。

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `batch_min` | 40 | 攒够多少条消息就抽取一次；群热闹时调大（60–80）省 token |
| `idle_secs` | 900 | 闲置多少秒后即使没攒够也抽取（兜底） |
| `min_gap_secs` | 300 | 两次抽取的最小间隔 |
| `extract_provider_id` | `deepseek/deepseek-flash` | 抽取用的小模型；留空用默认对话模型 |
| `inject_enabled` | true | 是否在聊天时注入记忆 |
| `keep_days` | 30 | 原始消息保留天数（印象与关系不受影响） |
| `recent_hours` | 6 | 私聊时带多少小时内的**群里**消息 |
| `interject_enabled` | true | 群里没 @ 时是否允许偶尔插话 |
| `interject_probability` | 10 | 每条群消息进入「要不要接话」判断的概率（%） |
| `interject_daily_limit` | 8 | 每群每天插话上限 |
| `interject_cooldown_min` | 25 | 两次插话最小间隔（分钟） |
| `interject_provider_id` | 空 | 插话判断用的模型，留空跟随抽取模型 |

> 小模型优先：抽取是「读一堆消息 → 写几十字」的活，`deepseek-flash` 这类足够，成本约为对话主模型的几十分之一。

---

## 工作原理

### 三张表

```sql
people(uid PK, nickname, cards, first_seen, last_seen, msg_count, impression, impression_at)
events(id PK, ts, umo, chat_kind, chat_id, speaker_uid, speaker_name, text, reply_to, processed)
relations(a_uid, b_uid, relation, confidence, evidence, updated_at, PK(a,b,relation))
```

- **身份按 QQ 号归一**：同一个人在群里、私聊里都是同一个 `uid`，昵称变了也不影响
- `events.reply_to` 记录引用回复的发送者，用来还原「谁在对谁说」
- `events` 默认 30 天滚动清理，`people` / `relations` 长期保留

### 注入顺序（重要）

AstrBot 的 `on_llm_request` 按 priority **从大到小**执行。本插件挂在 `priority=-300000`，
也就是**最后一个**改 prompt 的：

```
人设 / 系统提示 …… private_companion(-260000) 整段重写 system_prompt …… social_graph(-300000) 追加记忆
```

排早了会被陪伴类插件整段覆盖——这是踩过的坑，别调小这个数值。

### 隐私边界

- 私聊时，「最近别的会话里发生的事」**只带群聊**，不会把别人的私聊内容串出来
- 抽取后的印象与人设里的人一样，会被注入给模型；原始消息只留在本地库
- 一切都在本机：不联网、不入第三方，除了那次「攒够一批 → 调一次模型」

---

## 已知限制

- 印象是**自然语言一段话**，不是向量检索；人数多了（几百人）之后，靠「提到名字」匹配，不是语义搜索
- 同名/改名：按 uid 归一，但抽取模型只见过昵称，偶尔会把两个人写混
- 插话判断与抽取都用小模型，质量随模型起伏
- 好感度只读——要写回得改好感度插件的存储逻辑

---

## 文件结构

```
astrbot_plugin_social_graph/
├── main.py            # 插件主体：收听、抽取、注入、命令、Web API
├── store.py           # SQLite 封装（people / events / relations）
├── _conf_schema.json  # 面板配置项定义
├── metadata.yaml      # 插件元信息与页面声明
└── views/memory/      # WebUI 页面（index.html / app.js / style.css）
```

---

## 开发笔记

- 插件页面放 `views/<页面名>/index.html`（`views` 优先，`pages` 是别名），`metadata.yaml` 里用 `pages:` 声明 `name` / `title`
- 前端鉴权交给桥接：`window.AstrBotPluginPage` → `await bridge.ready()` → `bridge.apiGet("page/overview")` / `bridge.apiPost(...)`，endpoint 写插件内相对路径
- 后端：`context.register_web_api(f"/astrbot_plugin_social_graph/{endpoint}", handler, ["GET"|"POST"], desc)`，在 `initialize()` 里注册
- 数据路径用 `Path(__file__).resolve().parents[2] / "plugin_data"`，不依赖进程工作目录
- sqlite 连接以 `check_same_thread=False` 打开，页面接口的查询跑在 `asyncio.to_thread` 里

## 更新日志

- **0.2.0** — WebUI 面板（人物 / 关系网 / 消息流 / 设置）、好感度只读展示、`page/*` 接口
- **0.1.0** — 被动收听、批量抽取、注入、`/画像` `/关系` `/整理`

## License

MIT
