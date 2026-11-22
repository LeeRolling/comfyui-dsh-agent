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

function addQuestion(ev) {
  if (!S.messages) return;
  const questions = ev.questions || [];
  const card = el("div", "dsh-question");
  card.appendChild(el("div", "dsh-q-title", "Agent 需要你确认："));
  const answers = [];
  let remaining = questions.length;
  const sendIfDone = async () => {
    if (remaining > 0) return;
    card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    await postJSON("/dsh/answer", { requestId: ev.requestId, answers });
  };
  questions.forEach((q) => {
    const block = el("div", "dsh-q-block");
    if (q.header) block.appendChild(el("div", "dsh-q-header", q.header));
    block.appendChild(el("div", "dsh-q-text", q.question || ""));
    const options = q.options && q.options.length ? q.options : [{ label: "继续" }];
    const optWrap = el("div", "dsh-q-options");
    const answer = { id: q.id, selected: [] };
    answers.push(answer);
    let picked = false;
    options.forEach((opt) => {
      const btn = el("button", "dsh-q-opt", opt.label);
      if (opt.description) btn.title = opt.description;
      btn.onclick = async () => {
        if (picked) return;
        picked = true;
        answer.selected = [opt.label];
        btn.classList.add("dsh-picked");
        optWrap.querySelectorAll("button").forEach((b) => (b.disabled = true));
        block.appendChild(el("div", "dsh-q-picked", `已选择：${opt.label}`));
        remaining -= 1;
        await sendIfDone();
      };
      optWrap.appendChild(btn);
    });
    block.appendChild(optWrap);
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
  }
}

// ---------------------------------------------------------------- polling

function handleEvent(ev) {
  switch (ev.kind) {
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
  try {
    const data = await getJSON(`/dsh/poll?cursor=${S.cursor}`);
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
  }
}

function startPolling() {
  if (S.pollTimer) return;
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
  setThinking(true);
  postJSON("/dsh/chat", { text }).then((data) => {
    if (!data.ok) {
      setThinking(false);
      addMessage("error", data.error || "发送失败");
    }
  });
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
  const newChat = el("button", "dsh-icon-btn", "新对话");
  newChat.title = "结束当前对话并开启新会话";
  newChat.onclick = async () => {
    if (!confirm("开启新的对话？当前对话上下文会被清空。")) return;
    await postJSON("/dsh/reset", {});
    S.cursor = 0;
    S.lastGraphKey = "";
    S.toolChips.clear();
    if (S.messages) S.messages.innerHTML = "";
    addMessage("system", "已开启新对话。");
    pollOnce();
  };
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
  // Resume from the cursor we already hold. Resetting it to the newest event
  // (-1) silently skipped whatever the agent finished while the tab was shut.
  getJSON(`/dsh/poll?cursor=${S.cursor}`)
    .then((d) => {
      S.cursor = d.cursor ?? S.cursor;
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
