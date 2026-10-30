const bridge = window.AstrBotPluginPage;
const REQ_TIMEOUT = 25000;

const state = {
  people: [],
  selectedUid: "",
  person: null,
  tab: "people",
};

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function fmtTime(ts, withYear = false) {
  const n = Number(ts || 0);
  if (!n) return "—";
  const d = new Date(n * 1000);
  if (Number.isNaN(d.getTime())) return "—";
  const pad = (v) => String(v).padStart(2, "0");
  const md = `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return withYear ? `${d.getFullYear()}-${md} ${hm}` : `${md} ${hm}`;
}

function notify(message, kind = "") {
  const el = $("notice");
  el.textContent = message;
  el.className = `notice${kind ? " " + kind : ""}`;
  if (kind === "ok") {
    window.setTimeout(() => { el.classList.add("hidden"); }, 2600);
  }
}

function withTimeout(promise, ms = REQ_TIMEOUT) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = window.setTimeout(() => reject(new Error("请求超时，请刷新页面重试")), ms);
  });
  return Promise.race([promise, timeout]).finally(() => window.clearTimeout(timer));
}

function unwrap(result) {
  if (result && typeof result === "object") {
    if (result.status === "error") throw new Error(result.message || "请求失败");
    if (Object.prototype.hasOwnProperty.call(result, "ok")) {
      if (!result.ok) throw new Error(result.error || result.message || "请求失败");
      return result;
    }
  }
  return result || {};
}

async function apiGet(endpoint, params = {}) {
  return unwrap(await withTimeout(bridge.apiGet(endpoint, params)));
}

async function apiPost(endpoint, body = {}) {
  return unwrap(await withTimeout(bridge.apiPost(endpoint, body)));
}

/* ---------------- 渲染 ---------------- */

function renderStats(stats, config) {
  const s = stats || {};
  const bits = [
    `认识 ${s.people ?? 0} 人`,
    `有条目 ${s.with_impression ?? 0}`,
    `消息 ${s.events ?? 0} 条`,
    `待整理 ${s.unprocessed ?? 0}`,
    `关系 ${s.relations ?? 0}`,
  ];
  const inject = config && config.inject_enabled ? "记忆注入：开" : "记忆注入：关";
  $("statsLine").textContent = bits.join(" · ") + "　" + inject;
}

function renderPeople() {
  const box = $("peopleList");
  if (!state.people.length) {
    box.innerHTML = `<div class="empty">还没有记下任何人</div>`;
    return;
  }
  box.innerHTML = state.people.map((p) => {
    const imp = (p.impression || "").trim();
    const sub = imp || "（还没有印象）";
    const active = p.uid === state.selectedUid ? " active" : "";
    const badge = imp ? "" : `<span class="badge">空</span>`;
    const aff = p.affection != null ? `<span class="badge aff">♥ ${p.affection}</span>` : "";
    return `<div class="person-item${active}" data-uid="${esc(p.uid)}">
      <div class="avatar">${esc((p.nickname || p.uid || "?").slice(0, 1))}</div>
      <div class="info">
        <div class="name">${esc(p.nickname || p.uid)} ${badge}</div>
        <div class="sub">${esc(sub)}</div>
      </div>
      ${aff}
      <div class="badge">${p.msg_count ?? 0}</div>
    </div>`;
  }).join("");

  box.querySelectorAll(".person-item").forEach((el) => {
    el.addEventListener("click", () => selectPerson(el.dataset.uid));
  });
}

function renderPerson() {
  const p = state.person;
  const empty = $("personEmpty");
  const detail = $("personDetail");
  if (!p) {
    empty.classList.remove("hidden");
    detail.classList.add("hidden");
    return;
  }
  empty.classList.add("hidden");
  detail.classList.remove("hidden");

  $("pAvatar").textContent = (p.person.nickname || p.person.uid || "?").slice(0, 1);
  $("pNickname").value = p.person.nickname || "";
  $("pUid").textContent = p.person.uid;
  $("pCount").textContent = `${p.person.msg_count ?? 0} 条消息`;
  $("pAff").textContent = p.person.affection != null ? `好感度 ${p.person.affection}` : "";
  $("pSeen").textContent = `最后出现 ${fmtTime(p.person.last_seen, true)}`;
  $("pImpression").value = p.person.impression || "";
  $("pImpAt").textContent = p.person.impression_at ? `更新于 ${fmtTime(p.person.impression_at, true)}` : "";

  const relBox = $("pRelations");
  if (!p.relations || !p.relations.length) {
    relBox.innerHTML = `<div class="sub" style="color:var(--muted);font-size:12px">还没有推断出关系</div>`;
  } else {
    relBox.innerHTML = p.relations.map((r) => {
      const other = r.a_uid === p.person.uid ? r.b_name || r.b_uid : r.a_name || r.a_uid;
      return `<div class="rel-item">
        <span class="who">${esc(other)}</span>
        <span>${esc(r.relation)}</span>
        <span class="conf">${Number(r.confidence || 0).toFixed(2)}</span>
        <button class="icon-btn" data-del-rel="1"
          data-a="${esc(r.a_uid)}" data-b="${esc(r.b_uid)}" data-r="${esc(r.relation)}"
          title="删除">✕</button>
      </div>`;
    }).join("");
    relBox.querySelectorAll("[data-del-rel]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await apiPost("page/delete_relation", {
            a_uid: btn.dataset.a, b_uid: btn.dataset.b, relation: btn.dataset.r,
          });
          notify("关系已删除", "ok");
          await refreshPerson();
          await loadOverview();
        } catch (e) { notify(e.message, "error"); }
      });
    });
  }

  const evBox = $("pEvents");
  if (!p.events || !p.events.length) {
    evBox.innerHTML = `<div class="sub" style="color:var(--muted);font-size:12px">没有记录到消息</div>`;
  } else {
    evBox.innerHTML = p.events.map((ev) => {
      const where = ev.chat_kind === "group" ? `群${ev.chat_id}` : "私聊";
      return `<div class="event">
        <div class="ev-head">${esc(where)} · ${fmtTime(ev.ts, true)}</div>
        <div class="ev-body">${esc(ev.text)}</div>
      </div>`;
    }).join("");
  }
}

function renderRelations(rows) {
  const body = $("relBody");
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="6" style="color:var(--muted)">还没推断出任何关系</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((r) => `<tr>
    <td>${esc(r.a_name || r.a_uid)}</td>
    <td>${esc(r.relation)}</td>
    <td>${esc(r.b_name || r.b_uid)}</td>
    <td>${Number(r.confidence || 0).toFixed(2)}</td>
    <td class="ev">${esc((r.evidence || "").slice(0, 120))}</td>
    <td><button class="icon-btn" data-a="${esc(r.a_uid)}" data-b="${esc(r.b_uid)}" data-r="${esc(r.relation)}">✕</button></td>
  </tr>`).join("");
  body.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await apiPost("page/delete_relation", {
          a_uid: btn.dataset.a, b_uid: btn.dataset.b, relation: btn.dataset.r,
        });
        notify("关系已删除", "ok");
        await loadRelations();
        await loadOverview();
      } catch (e) { notify(e.message, "error"); }
    });
  });
}

function renderFeed(rows) {
  const box = $("feedList");
  if (!rows.length) {
    box.innerHTML = `<div class="empty">没有消息</div>`;
    return;
  }
  box.innerHTML = rows.map((ev) => {
    const where = ev.chat_kind === "group" ? `群${ev.chat_id}` : "私聊";
    const flag = ev.processed ? "" : `<span class="badge">未整理</span>`;
    return `<div class="event">
      <div class="ev-head"><span class="who">${esc(ev.speaker_name || ev.speaker_uid)}</span>
        · ${esc(where)} · ${fmtTime(ev.ts, true)} ${flag}</div>
      <div class="ev-body">${esc(ev.text)}</div>
    </div>`;
  }).join("");
}

function fillConfigForm(cfg) {
  const form = $("configForm");
  form.querySelectorAll("[name]").forEach((el) => {
    const key = el.name;
    if (!(key in cfg)) return;
    if (el.type === "checkbox") el.checked = !!cfg[key];
    else el.value = cfg[key];
  });
}

/* ---------------- 数据 ---------------- */

async function loadOverview() {
  const q = ($("search").value || "").trim();
  const data = await apiGet("page/overview", q ? { q } : {});
  state.people = data.people || [];
  renderStats(data.stats, data.config);
  renderPeople();
  if (data.config) fillConfigForm(data.config);
}

async function selectPerson(uid) {
  state.selectedUid = uid;
  renderPeople();
  await refreshPerson();
}

async function refreshPerson() {
  if (!state.selectedUid) { state.person = null; renderPerson(); return; }
  const data = await apiGet("page/person", { uid: state.selectedUid });
  state.person = data;
  renderPerson();
}

async function loadRelations() {
  const data = await apiGet("page/relations");
  const rows = (data.relations || []).slice().sort(
    (x, y) => Number(y.confidence || 0) - Number(x.confidence || 0)
  );
  renderRelations(rows);
}

async function loadFeed() {
  const chatId = ($("feedChat").value || "").trim();
  const data = await apiGet("page/feed", chatId ? { chat_id: chatId, limit: 120 } : { limit: 120 });
  renderFeed(data.events || []);
}

/* ---------------- 交互 ---------------- */

function bindEvents() {
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", async () => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
      state.tab = btn.dataset.tab;
      document.querySelectorAll(".pane").forEach((p) => {
        p.classList.toggle("active", p.dataset.pane === state.tab);
      });
      try {
        if (state.tab === "relations") await loadRelations();
        if (state.tab === "feed") await loadFeed();
      } catch (e) { notify(e.message, "error"); }
    });
  });

  let searchTimer = null;
  $("search").addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(async () => {
      try { await loadOverview(); } catch (e) { notify(e.message, "error"); }
    }, 260);
  });

  $("refreshBtn").addEventListener("click", async () => {
    try {
      await loadOverview();
      await refreshPerson();
      notify("已刷新", "ok");
    } catch (e) { notify(e.message, "error"); }
  });

  $("extractBtn").addEventListener("click", async () => {
    const btn = $("extractBtn");
    btn.disabled = true;
    btn.textContent = "整理中…";
    try {
      const data = await apiPost("page/extract", {});
      notify(data.message || "整理完成", "ok");
      await loadOverview();
      await refreshPerson();
    } catch (e) {
      notify(e.message, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "立刻整理";
    }
  });

  $("savePerson").addEventListener("click", async () => {
    if (!state.person) return;
    try {
      await apiPost("page/save_person", {
        uid: state.person.person.uid,
        nickname: $("pNickname").value,
        impression: $("pImpression").value,
      });
      notify("已保存", "ok");
      await refreshPerson();
      await loadOverview();
    } catch (e) { notify(e.message, "error"); }
  });

  $("deletePerson").addEventListener("click", async () => {
    if (!state.person) return;
    const name = state.person.person.nickname || state.person.person.uid;
    if (!window.confirm(`删除「${name}」的全部记忆（印象 + 关系 + 消息）？`)) return;
    try {
      await apiPost("page/delete_person", { uid: state.person.person.uid, drop_events: true });
      state.selectedUid = "";
      state.person = null;
      renderPerson();
      notify("已删除", "ok");
      await loadOverview();
    } catch (e) { notify(e.message, "error"); }
  });

  $("relForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!state.person) return;
    const other = ($("relOther").value || "").trim();
    const rel = ($("relText").value || "").trim();
    const conf = Number($("relConf").value || 0.6);
    if (!other || !rel) { notify("填一下对方 QQ 和关系", "error"); return; }
    try {
      await apiPost("page/save_relation", {
        a_uid: state.person.person.uid, b_uid: other, relation: rel, confidence: conf,
      });
      $("relOther").value = "";
      $("relText").value = "";
      notify("关系已添加", "ok");
      await refreshPerson();
    } catch (e) { notify(e.message, "error"); }
  });

  $("feedReload").addEventListener("click", async () => {
    try { await loadFeed(); } catch (e) { notify(e.message, "error"); }
  });

  $("configForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const form = $("configForm");
    const body = {};
    form.querySelectorAll("[name]").forEach((el) => {
      if (el.type === "checkbox") body[el.name] = el.checked;
      else if (el.value !== "") body[el.name] = el.value;
    });
    try {
      const data = await apiPost("page/save_config", body);
      fillConfigForm(data.config || body);
      notify("设置已保存", "ok");
      await loadOverview();
    } catch (e) { notify(e.message, "error"); }
  });
}

async function init() {
  if (!bridge) {
    notify("没有检测到 AstrBot 页面桥接，请从 AstrBot WebUI 的插件页面进入。", "error");
    return;
  }
  try {
    await withTimeout(bridge.ready(), 8000);
  } catch (e) {
    notify(e.message || "桥接初始化失败", "error");
    return;
  }
  bindEvents();
  try {
    await loadOverview();
  } catch (e) {
    notify(e.message, "error");
  }
}

init();
