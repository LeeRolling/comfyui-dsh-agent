import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// ComfyUI serves WEB_DIRECTORY files but does not auto-apply CSS.
try {
  const cssHref = new URL("./css/dsh_agent.css", import.meta.url).href;
  if (!document.querySelector(`link[data-dsh-agent-css]`)) {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = cssHref;
    link.dataset.dshAgentCss = "1";
    document.head.appendChild(link);
  }
} catch (err) {
  console.warn("[dsh-agent] failed to load css", err);
}

const S = {
  cursor: 0,
  pollTimer: null,
  graphTimer: null,
  engine: "unknown",
  running: false,
  hasGraph: false,
  lastError: null,
  lastGraphKey: "",
  errorBanner: null,
  root: null,
  panelKids: null,
  messages: null,
  composer: null,
  statusEl: null,
  lastAssistantEl: null,
  toolChips: new Map(),
  // 刚由本机乐观渲染过的那句用户消息；服务端回显同一条时用它去重（否则面板里会出现两条一样的）
  lastLocalSend: null,
  activeSession: "",
  sessions: [],
  trash: [],
  menuEl: null,
};

const MAX_MESSAGES = 200;

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderMarkdownLite(text) {
  const parts = String(text).split(/```/);
  let html = "";
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      const nl = part.indexOf("\n");
      const code = nl >= 0 ? part.slice(nl + 1) : part;
      html += `<pre class="dsh-code">${escapeHtml(code.replace(/\n$/, ""))}</pre>`;
    } else {
      let chunk = escapeHtml(part);
      chunk = chunk.replace(/`([^`\n]+)`/g, "<code>$1</code>");
      chunk = chunk.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
      chunk = chunk.replace(/\n/g, "<br>");
      html += chunk;
    }
  });
  return html;
}

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  let data = {};
  try {
    data = await resp.json();
  } catch {
    data = { ok: false, error: `HTTP ${resp.status}` };
  }
  return data;
}

async function getJSON(url) {
  const resp = await fetch(url);
  return resp.json();
}

// ---------------------------------------------------------------- UI

function addMessage(kind, text, opts = {}) {
  if (!S.messages) return null;
  const row = el("div", `dsh-msg dsh-${kind}`);
  if (kind === "assistant") {
    row.innerHTML = `<div class="dsh-md">${renderMarkdownLite(text)}</div>`;
    S.lastAssistantEl = row;
  } else if (kind === "user") {
    row.textContent = text;
  } else if (kind === "error") {
    row.innerHTML = `<span class="dsh-err-icon">!</span><span>${escapeHtml(text)}</span>`;
  } else if (kind === "system") {
    row.textContent = text;
  } else {
    row.textContent = text;
  }
  S.messages.appendChild(row);
  while (S.messages.children.length > MAX_MESSAGES) {
    S.messages.removeChild(S.messages.firstChild);
  }
  S.messages.scrollTop = S.messages.scrollHeight;
  return row;
}

function addToolChip(name, detail, callId) {
  if (!S.messages) return null;
  const row = el("div", "dsh-tool");
  row.innerHTML = `<span class="dsh-tool-dot"></span><span class="dsh-tool-name">${escapeHtml(
    name
  )}</span>${detail ? `<span class="dsh-tool-detail">${escapeHtml(detail)}</span>` : ""}`;
  S.messages.appendChild(row);
  S.messages.scrollTop = S.messages.scrollHeight;
  if (callId) S.toolChips.set(callId, row);
  return row;
}

function resolveToolChip(callId, ok, detail) {
  const row = callId ? S.toolChips.get(callId) : null;
  if (!row) {
    if (!ok && detail) addToolChip("✗ 失败", detail);
    return;
  }
  row.classList.add(ok ? "dsh-tool-ok" : "dsh-tool-err");
  if (!ok && detail) {
    let extra = row.querySelector(".dsh-tool-error");
    if (!extra) {
      extra = el("span", "dsh-tool-error");
      row.appendChild(extra);
    }
    extra.textContent = detail;
  }
}

function setThinking(on) {
  if (!S.statusEl) return;
  S.statusEl.classList.toggle("dsh-thinking", !!on);
  S.statusEl.textContent = on ? "思考中…" : "";
}

function updateErrorBanner() {
  if (!S.errorBanner) return;
  if (S.lastError) {
    const msg = S.lastError.exception_message || S.lastError.message || "执行出错";
    const node = S.lastError.node_type ? `${S.lastError.node_type} #${S.lastError.node_id}` : "";
    S.errorBanner.style.display = "flex";
    S.errorBanner.querySelector(".dsh-banner-text").textContent = `执行报错：${node} ${msg}`;
  } else {
    S.errorBanner.style.display = "none";
  }
}

function markCardClosed(requestId, note) {
  // 不把 requestId 拼进选择器：服务端给的 id 含引号时会抛 SyntaxError，
  // 进而吞掉整批事件。直接按 dataset 比对更稳。
  const cards = S.messages ? Array.from(S.messages.children).filter((n) => n.dataset && n.dataset.req === requestId) : [];
  cards.forEach((card) => {
    card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    card.querySelectorAll("input").forEach((i) => (i.disabled = true));
    card.classList.add("dsh-card-closed");
    if (note) {
      const tag = el("div", "dsh-card-note", note);
      card.appendChild(tag);
    }
  });
}

function addApproval(ev) {
  if (!S.messages) return;
  const card = el("div", "dsh-approval");
  card.dataset.req = ev.requestId;
  card.appendChild(el("div", "dsh-q-title", "Agent 想运行一个工具："));
  const head = el("div", "dsh-approval-tool");
  head.appendChild(el("span", "dsh-tool-name", ev.toolName || "tool"));
  if (ev.callId) head.appendChild(el("span", "dsh-tool-callid", ev.callId.slice(0, 8)));
  card.appendChild(head);
  if (ev.reason) card.appendChild(el("div", "dsh-q-text", ev.reason));

  const decide = async (outcome, remember) => {
    card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    const res = await postJSON("/dsh/approve", {
      requestId: ev.requestId,
      outcome,
      remember: !!remember,
    });
    const label =
      outcome === "allowed-once"
        ? remember
          ? "已允许，且本会话后续都放行"
          : "已允许这一次"
        : "已拒绝";
    card.appendChild(el("div", "dsh-q-picked", res && res.ok ? label : "这张卡已失效"));
  };

  const wrap = el("div", "dsh-q-options");
  const ok = el("button", "dsh-q-opt dsh-approve", "允许一次");
  ok.onclick = () => decide("allowed-once", false);
  const always = el("button", "dsh-q-opt", "本会话总是允许");
  always.title = "之后这个会话里的工具调用都不再问";
  always.onclick = () => decide("allowed-once", true);
  const no = el("button", "dsh-q-opt dsh-reject", "拒绝");
  no.onclick = () => decide("rejected", false);
  wrap.appendChild(ok);
  wrap.appendChild(always);
  wrap.appendChild(no);
  card.appendChild(wrap);
  S.messages.appendChild(card);
  S.messages.scrollTop = S.messages.scrollHeight;
}

function addQuestion(ev) {
  if (!S.messages) return;
  const questions = ev.questions || [];
  const card = el("div", "dsh-question");
  card.dataset.req = ev.requestId;
  card.appendChild(el("div", "dsh-q-title", "Agent 需要你确认："));
  const answers = [];
  let remaining = questions.length;
  const sendIfDone = async () => {
    if (remaining > 0) return;
    card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    const res = await postJSON("/dsh/answer", { requestId: ev.requestId, answers });
    if (res && !res.ok) card.appendChild(el("div", "dsh-q-picked", "提交失败：卡片可能已过期"));
  };
  questions.forEach((q) => {
    const block = el("div", "dsh-q-block");
    if (q.header) block.appendChild(el("div", "dsh-q-header", q.header));
    block.appendChild(el("div", "dsh-q-text", q.question || ""));
    const options = q.options && q.options.length ? q.options : [];
    const optWrap = el("div", "dsh-q-options");
    const answer = { id: q.id, selected: [] };
    answers.push(answer);
    let picked = false;
    const settle = async (label) => {
      picked = true;
      block.querySelectorAll("button").forEach((b) => (b.disabled = true));
      const input = block.querySelector(".dsh-q-custom");
      if (input) input.disabled = true;
      block.appendChild(el("div", "dsh-q-picked", `已答：${label}`));
      remaining -= 1;
      await sendIfDone();
    };
    options.forEach((opt) => {
      const btn = el("button", "dsh-q-opt", opt.label);
      if (opt.description) btn.title = opt.description;
      btn.onclick = async () => {
        if (picked) return;
        answer.selected = [opt.label];
        await settle(opt.label);
      };
      optWrap.appendChild(btn);
    });
    if (options.length) block.appendChild(optWrap);

    // 自定义回答：协议允许 selected 为空 + custom 文本
    const customWrap = el("div", "dsh-q-custom-wrap");
    const input = el("input", "dsh-q-custom");
    input.placeholder = options.length ? "也可以自己写一个答案…" : "输入你的回答…";
    input.type = "text";
    const submit = el("button", "dsh-q-opt dsh-q-custom-btn", "提交");
    submit.onclick = async () => {
      if (picked) return;
      const text = (input.value || "").trim();
      if (!text) {
        input.focus();
        return;
      }
      answer.selected = [];
      answer.custom = text;
      await settle(text);
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        submit.click();
      }
    });
    customWrap.appendChild(input);
    customWrap.appendChild(submit);
    block.appendChild(customWrap);
    card.appendChild(block);
  });
  S.messages.appendChild(card);
  S.messages.scrollTop = S.messages.scrollHeight;
}

async function applyPendingGraph(pending) {
  if (!pending || !pending.graph) return;
  const previous = app.graph ? app.graph.serialize() : null;
  addMessage("system", `Agent 提交了新画布：${pending.reason}（${pending.nodes} 节点 / ${pending.links} 连线）`);
  try {
    await app.loadGraphData(pending.graph);
    await postJSON("/dsh/pending/ack", { id: pending.id });
    if (previous) {
      const undo = el("button", "dsh-undo", "撤销这次画布修改");
      undo.onclick = async () => {
        await app.loadGraphData(previous);
        undo.disabled = true;
        undo.textContent = "已撤销";
      };
      S.messages.appendChild(undo);
      S.messages.scrollTop = S.messages.scrollHeight;
    }
  } catch (err) {
    addMessage("error", `加载 Agent 提交的画布失败：${err}`);
    // 失败的图也要 ack：否则服务端每 700ms 重投同一份坏图，消息区会被刷屏。
    await postJSON("/dsh/pending/ack", { id: pending.id }).catch(() => {});
  }
}

// ---------------------------------------------------------------- polling

function handleEvent(ev) {
  switch (ev.kind) {
    case "user":
      if (ev.text && ev.text.trim()) {
        const t = ev.text.trim();
        const local = S.lastLocalSend;
        if (local && local.text === t && Date.now() - local.at < 20000) {
          // 这句就是我刚发出去的，本机已经渲染过一次，跳过回显
          S.lastLocalSend = null;
        } else {
          addMessage("user", t);
        }
      }
      break;
    case "assistant":
      if (ev.text && ev.text.trim()) addMessage("assistant", ev.text);
      break;
    case "tool":
      addToolChip(ev.name || "tool", ev.args ? ev.args.slice(0, 90) : "", ev.callId);
      break;
    case "tool_result":
      resolveToolChip(ev.callId, ev.ok, ev.error || ev.preview || "");
      break;
    case "error":
      addMessage("error", ev.text || "未知错误");
      break;
    case "activity":
      addMessage("system", ev.text || "");
      break;
    case "question":
      addQuestion(ev);
      break;
    case "question_closed":
      markCardClosed(ev.requestId, "这张卡已失效");
      break;
    case "approval":
      addApproval(ev);
      break;
    case "approval_closed":
      markCardClosed(ev.requestId, ev.text || (ev.outcome === "rejected" ? "已拒绝" : "已处理"));
      break;
    case "ready":
      addMessage("system", `DeepSeek Harness 已连接（${ev.text || "deepseek"}）`);
      break;
    case "runtime_stopped":
      setThinking(false);
      if (ev.text) addMessage("error", ev.text);
      break;
    case "title":
      break;
    default:
      break;
  }
  if (ev.kind === "status") {
    S.running = ev.status === "running";
    setThinking(S.running);
  }
}

async function pollOnce() {
  // 一次只允许一个在途请求：慢响应叠加快轮询会让游标来回跳。
  if (S.pollInFlight) return;
  const reqSid = S.activeSession || "";
  S.pollInFlight = true;
  try {
    const sid = reqSid ? `&sessionId=${encodeURIComponent(reqSid)}` : "";
    const data = await getJSON(`/dsh/poll?cursor=${S.cursor}${sid}`);
    // 请求期间用户切了会话：这份响应属于旧会话，丢弃，别把游标/消息串过去。
    if ((S.activeSession || "") !== reqSid) return;
    S.cursor = data.cursor ?? S.cursor;
    S.engine = data.status || S.engine;
    S.running = !!data.running;
    setThinking(S.running);
    (data.events || []).forEach(handleEvent);
    if (data.pendingGraph) await applyPendingGraph(data.pendingGraph);
    if (S.statusDot) {
      S.statusDot.className = "dsh-dot dsh-dot-" + (data.status || "unknown");
      S.statusDot.title = `DSH runtime: ${data.status || "unknown"}`;
    }
    if (data.status === "error" && data.error) {
      if (!S._lastEngineError || S._lastEngineError !== data.error) {
        S._lastEngineError = data.error;
        addMessage("error", data.error);
      }
    }
  } catch {
    /* ComfyUI may be reloading; ignore */
  } finally {
    S.pollInFlight = false;
  }
}

function startPolling() {
  if (S.pollTimer) {
    clearInterval(S.pollTimer);
  }
  S.pollTimer = setInterval(pollOnce, 700);
  pollOnce();
}

// ---------------------------------------------------------------- graph sync

async function syncGraph() {
  if (!app.graph || document.hidden) return;
  let graph;
  try {
    graph = app.graph.serialize();
  } catch {
    return;
  }
  const key = JSON.stringify(graph);
  if (key === S.lastGraphKey) return;
  S.lastGraphKey = key;
  await postJSON("/dsh/graph", { graph });
}

function startGraphSync() {
  if (S.graphTimer) return;
  S.graphTimer = setInterval(syncGraph, 1800);
  setTimeout(syncGraph, 400);
}

// ---------------------------------------------------------------- composer

function sendCurrent() {
  const input = S.composer;
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  input.style.height = "";
  addMessage("user", text);
  S.lastLocalSend = { text, at: Date.now() };
  setThinking(true);
  postJSON("/dsh/chat", { text, sessionId: S.activeSession || undefined }).then((data) => {
    if (!data.ok) {
      setThinking(false);
      addMessage("error", data.error || "发送失败");
    }
  });
}

// ---------------------------------------------------------------- sessions

function relTime(ts) {
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - (ts || 0)));
  if (sec < 60) return "刚刚";
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`;
  if (sec < 86400 * 30) return `${Math.floor(sec / 86400)} 天前`;
  return new Date((ts || 0) * 1000).toLocaleDateString();
}

function closeHistoryMenu() {
  if (S.menuEl) {
    S.menuEl.remove();
    S.menuEl = null;
  }
}

function menuStyle() {
  return [
    "position:absolute",
    "left:10px",
    "top:46px",
    "z-index:80",
    "min-width:280px",
    "max-width:360px",
    "max-height:62vh",
    "overflow:auto",
    "background:#202027",
    "border:1px solid #3b3b46",
    "border-radius:10px",
    "box-shadow:0 10px 28px rgba(0,0,0,.5)",
    "padding:6px",
    "font-size:12px",
    "color:#dcdce4",
  ].join(";");
}

function menuRow(active) {
  const row = el("div", "dsh-menu-row");
  row.style.cssText = [
    "display:flex",
    "align-items:center",
    "gap:6px",
    "padding:7px 8px",
    "border-radius:7px",
    "cursor:pointer",
    active ? "background:#2c2c38" : "",
  ].join(";");
  row.onmouseenter = () => (row.style.background = "#2c2c38");
  row.onmouseleave = () => (row.style.background = active ? "#2c2c38" : "transparent");
  return row;
}

function openHistoryMenu(anchor) {
  if (S.menuEl) {
    closeHistoryMenu();
    return;
  }
  const root = S.root;
  if (root) root.style.position = "relative";
  const menu = el("div", "dsh-menu");
  menu.style.cssText = menuStyle();

  const head = el("div", "", "历史会话");
  head.style.cssText = "padding:6px 8px;font-weight:600;opacity:.85";
  menu.appendChild(head);

  if (!S.sessions.length) {
    const empty = el("div", "", "（还没有别的会话）");
    empty.style.cssText = "padding:6px 8px;opacity:.6";
    menu.appendChild(empty);
  }

  for (const item of S.sessions) {
    const active = item.sessionId === S.activeSession;
    const row = menuRow(active);
    const label = el("div", "", `${active ? "● " : ""}${item.title || "未命名"}`);
    label.style.cssText = "flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
    const meta = el("div", "", `${item.count || 0} 条 · ${relTime(item.updatedAt)}`);
    meta.style.cssText = "opacity:.55;font-size:11px;margin-top:2px";
    const textWrap = el("div", "");
    textWrap.style.cssText = "flex:1;min-width:0";
    textWrap.appendChild(label);
    textWrap.appendChild(meta);
    textWrap.onclick = () => switchSession(item);
    row.appendChild(textWrap);

    const rename = el("button", "dsh-mini", "✎");
    rename.title = "改名";
    rename.style.cssText = "background:none;border:none;color:#bbb;cursor:pointer;font-size:13px";
    rename.onclick = async (e) => {
      e.stopPropagation();
      const next = prompt("给这个会话起个名字：", item.title || "");
      if (!next || !next.trim()) return;
      await postJSON("/dsh/session/rename", { sessionId: item.sessionId, title: next.trim() });
      await refreshSessions();
      closeHistoryMenu();
      openHistoryMenu();
    };
    row.appendChild(rename);

    const del = el("button", "dsh-mini", "🗑");
    del.title = "移到回收站";
    del.style.cssText = "background:none;border:none;color:#bbb;cursor:pointer;font-size:12px";
    del.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`把「${item.title || "未命名"}」移到回收站？之后可以从回收站恢复。`)) return;
      const res = await postJSON("/dsh/session/trash", { sessionId: item.sessionId });
      await refreshSessions();
      closeHistoryMenu();
      if (res.next) await switchSession({ sessionId: res.next, title: "" }, true);
      else openHistoryMenu();
    };
    row.appendChild(del);
    menu.appendChild(row);
  }

  const sep = el("div", "");
  sep.style.cssText = "height:1px;background:#34343d;margin:6px 4px";
  menu.appendChild(sep);

  const trashBtn = el("div", "", `回收站 (${S.trash.length}) ›`);
  trashBtn.style.cssText = "padding:6px 8px;cursor:pointer;opacity:.85";
  trashBtn.onclick = () => openTrashMenu();
  menu.appendChild(trashBtn);

  menu.addEventListener("click", (e) => e.stopPropagation());
  (S.root || document.body).appendChild(menu);
  S.menuEl = menu;
}

function openTrashMenu() {
  closeHistoryMenu();
  const menu = el("div", "dsh-menu");
  menu.style.cssText = menuStyle();
  const head = el("div", "", `回收站（${S.trash.length}）`);
  head.style.cssText = "padding:6px 8px;font-weight:600;opacity:.85";
  menu.appendChild(head);

  if (!S.trash.length) {
    const empty = el("div", "", "（空的）");
    empty.style.cssText = "padding:6px 8px;opacity:.6";
    menu.appendChild(empty);
  }
  for (const item of S.trash) {
    const row = menuRow(false);
    const label = el("div", "", item.title || "未命名");
    label.style.cssText = "flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
    row.appendChild(label);
    const back = el("button", "", "恢复");
    back.style.cssText = "background:#2f3a33;border:1px solid #3f5247;color:#bcd;border-radius:6px;padding:2px 8px;cursor:pointer";
    back.onclick = async (e) => {
      e.stopPropagation();
      await postJSON("/dsh/session/restore", { sessionId: item.sessionId });
      await refreshSessions();
      openTrashMenu();
    };
    row.appendChild(back);
    const purge = el("button", "", "彻底删除");
    purge.style.cssText = "background:#3a2a2a;border:1px solid #5a3a3a;color:#f0b8b8;border-radius:6px;padding:2px 8px;cursor:pointer";
    purge.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm("彻底删除后无法恢复，确定？")) return;
      await postJSON("/dsh/session/purge", { sessionId: item.sessionId });
      await refreshSessions();
      openTrashMenu();
    };
    row.appendChild(purge);
    menu.appendChild(row);
  }

  if (S.trash.length) {
    const sep = el("div", "");
    sep.style.cssText = "height:1px;background:#34343d;margin:6px 4px";
    menu.appendChild(sep);
    const clear = el("div", "", "清空回收站");
    clear.style.cssText = "padding:6px 8px;cursor:pointer;color:#f0b8b8";
    clear.onclick = async () => {
      if (!confirm("清空回收站？所有条目会被彻底删除，无法恢复。")) return;
      await postJSON("/dsh/session/purge", { sessionId: "__all__" });
      await refreshSessions();
      closeHistoryMenu();
    };
    menu.appendChild(clear);
  }

  const back = el("div", "", "‹ 返回会话列表");
  back.style.cssText = "padding:6px 8px;cursor:pointer;opacity:.7";
  back.onclick = () => {
    closeHistoryMenu();
    openHistoryMenu();
  };
  menu.appendChild(back);

  menu.addEventListener("click", (e) => e.stopPropagation());
  (S.root || document.body).appendChild(menu);
  S.menuEl = menu;
}

async function refreshSessions() {
  try {
    const data = await getJSON("/dsh/sessions");
    S.sessions = data.sessions || [];
    S.trash = data.trash || [];
    // 只在还没选中会话时采纳服务端的 active：否则后台的一次切换会
    // 把用户正看着的会话指针拽走（列表高亮与视图对不上）。
    if (!S.activeSession && data.active) S.activeSession = data.active;
    if (S.historyBtn) {
      S.historyBtn.textContent = `历史会话 (${S.sessions.length}) ▾`;
    }
  } catch {
    /* ignore */
  }
}

async function switchSession(item, silent = false) {
  closeHistoryMenu();
  if (!item || !item.sessionId) return;
  if (item.sessionId === S.activeSession) return;
  if (!silent) {
    const ok = confirm(
      "切换到另一个会话？\n\n" +
        "旧会话如果还在跑任务，它会继续在后台跑完（不会中断），但两个会话同时跑会一起吃显存和速度。\n" +
        "机器扛得住就继续，扛不住可以先等它跑完。"
    );
    if (!ok) return;
  }
  const res = await postJSON("/dsh/session/select", { sessionId: item.sessionId });
  if (!res.ok) {
    alert(res.error || "切换失败");
    return;
  }
  S.activeSession = res.sessionId || item.sessionId;
  S.cursor = 0;
  S.toolChips.clear();
  S.lastGraphKey = "";
  if (S.messages) {
    S.messages.innerHTML = "";
    addMessage("system", `已切换到「${item.title || "会话"}」。`);
  }
  await refreshSessions();
  pollOnce();
}

function buildPanel(root) {
  root.classList.add("dsh-root");
  // The sidebar store removes a tab's DOM when it is toggled closed and calls
  // render() again when it is reopened. Reuse the nodes we already built so the
  // conversation stays visible instead of being wiped by a second build.
  if (S.panelKids) {
    const kids = S.panelKids;
    const attached = kids.length > 0 && kids[0].parentNode === root;
    if (!attached) {
      root.innerHTML = "";
      for (const node of kids) root.appendChild(node);
    }
    S.root = root;
    startGraphSync();
    refreshSessions();
    startPolling();
    if (S.messages) S.messages.scrollTop = S.messages.scrollHeight;
    return;
  }
  root.innerHTML = "";

  const header = el("div", "dsh-header");
  const title = el("div", "dsh-title", "DSH Agent");
  S.statusDot = el("span", "dsh-dot dsh-dot-unknown");
  const statusWrap = el("div", "dsh-status-wrap");
  statusWrap.appendChild(S.statusDot);
  S.statusEl = el("span", "dsh-status");
  statusWrap.appendChild(S.statusEl);

  const actions = el("div", "dsh-actions");
  S.historyBtn = el("button", "dsh-icon-btn", "历史会话 ▾");
  S.historyBtn.title = "切换历史会话 / 回收站";
  S.historyBtn.onclick = (e) => {
    e.stopPropagation();
    openHistoryMenu(S.historyBtn);
  };
  const newChat = el("button", "dsh-icon-btn", "新对话");
  newChat.title = "开一个新会话（旧会话保留在历史里）";
  newChat.onclick = async () => {
    const res = await postJSON("/dsh/session/new", {});
    if (!res.ok) {
      addMessage("error", res.error || "新建会话失败");
      return;
    }
    S.activeSession = res.sessionId;
    S.cursor = 0;
    S.lastGraphKey = "";
    S.toolChips.clear();
    if (S.messages) S.messages.innerHTML = "";
    addMessage("system", "已开启新对话。");
    await refreshSessions();
    pollOnce();
  };
  actions.appendChild(S.historyBtn);
  actions.appendChild(newChat);
  header.appendChild(title);
  header.appendChild(statusWrap);
  header.appendChild(actions);

  S.errorBanner = el("div", "dsh-banner");
  S.errorBanner.style.display = "none";
  const bannerText = el("div", "dsh-banner-text");
  const bannerBtn = el("button", "dsh-banner-btn", "让 Agent 分析");
  bannerBtn.onclick = () => {
    if (!S.lastError) return;
    const err = S.lastError;
    const text =
      `我的 ComfyUI 执行报错了，请帮我定位原因并修复：\n` +
      `节点: ${err.node_type || "?"} #${err.node_id ?? "?"}\n` +
      `异常: ${err.exception_type || ""} ${err.exception_message || ""}\n` +
      (err.traceback ? `\n\`\`\`\n${String(err.traceback).slice(-3000)}\n\`\`\`` : "");
    S.composer.value = text;
    sendCurrent();
  };
  S.errorBanner.appendChild(bannerText);
  S.errorBanner.appendChild(bannerBtn);

  S.messages = el("div", "dsh-messages");
  addMessage(
    "system",
    "这是运行在 ComfyUI 里的 DeepSeek Harness 助手。可以直接问报错、让我看画布，或让我重排节点。"
  );

  const composerWrap = el("div", "dsh-composer");
  S.composer = el("textarea", "dsh-input");
  S.composer.rows = 1;
  S.composer.placeholder = "描述你的需求，或粘贴报错…（Enter 发送 / Shift+Enter 换行）";
  S.composer.addEventListener("input", () => {
    S.composer.style.height = "auto";
    S.composer.style.height = Math.min(S.composer.scrollHeight, 160) + "px";
  });
  S.composer.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendCurrent();
    }
  });
  const sendBtn = el("button", "dsh-send", "发送");
  sendBtn.onclick = sendCurrent;
  composerWrap.appendChild(S.composer);
  composerWrap.appendChild(sendBtn);

  root.appendChild(header);
  root.appendChild(S.errorBanner);
  root.appendChild(S.messages);
  root.appendChild(composerWrap);

  S.root = root;
  S.panelKids = Array.from(root.childNodes);

  startGraphSync();
  refreshSessions();
  document.addEventListener("click", () => closeHistoryMenu());
  // Resume from the cursor we already hold. Resetting it to the newest event
  // (-1) silently skipped whatever the agent finished while the tab was shut.
  getJSON(`/dsh/poll?cursor=${S.cursor}`)
    .then((d) => {
      S.cursor = d.cursor ?? S.cursor;
      if (d.sessionId) S.activeSession = d.sessionId;
    })
    .catch(() => {})
    .finally(() => startPolling());
}

// ---------------------------------------------------------------- extension

app.registerExtension({
  name: "dsh.agent",
  async setup() {
    const register = () => {
      if (!app.extensionManager || !app.extensionManager.registerSidebarTab) return false;
      app.extensionManager.registerSidebarTab({
        id: "dsh-agent",
        icon: "pi pi-comments",
        title: "DSH",
        tooltip: "DeepSeek Harness Agent",
        type: "custom",
        render: (element) => buildPanel(element),
      });
      return true;
    };

    if (!register()) {
      // Fallback: floating drawer for frontends without the sidebar API.
      const btn = el("button", "dsh-fab", "DSH");
      const drawer = el("div", "dsh-drawer");
      drawer.style.display = "none";
      btn.onclick = () => {
        drawer.style.display = drawer.style.display === "none" ? "flex" : "none";
        if (drawer.style.display === "flex" && !drawer.dataset.built) {
          drawer.dataset.built = "1";
          buildPanel(drawer);
        }
      };
      document.body.appendChild(btn);
      document.body.appendChild(drawer);
    }

    api.addEventListener("execution_error", (event) => {
      S.lastError = event.detail || {};
      updateErrorBanner();
      postJSON("/dsh/error", S.lastError);
    });
    api.addEventListener("execution_interrupted", (event) => {
      S.lastError = { message: "执行被中断", ...(event.detail || {}) };
      updateErrorBanner();
      postJSON("/dsh/error", S.lastError);
    });
    api.addEventListener("execution_success", () => {
      S.lastError = null;
      updateErrorBanner();
    });
  },
});
