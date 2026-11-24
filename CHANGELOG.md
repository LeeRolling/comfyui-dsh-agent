# Changelog

首个公开版本基于原作者在 macOS 上的实现；本仓库在其之上补了 Windows 适配与面板上下文修复。

## v0.2.1 — 2026-11-24

### 修复：自己发的消息在面板里显示成两条

- 现象：每发一条消息，消息区出现两个一模一样的气泡。
- 原因：发送时前端先乐观渲染一次（不等服务端），引擎随后推同一句的 `user` 事件用于历史持久化，`handleEvent` 又把它画了一遍。
- 影响面：**只影响显示**。落盘每个会话只有一条 `user` 事件，模型每次也只收到一个 prompt，上下文没有重复。
- 修法（仅 `web/dsh_agent.js`）：发送时记下 `S.lastLocalSend = { text, at }`；回显文本相同且在 20 秒内则跳过渲染，避免重复绘制。
- 已知边界：已经画出来的旧气泡不会消失，切一次会话或刷新页面会按存盘内容重画（此时只有一条）。

## v0.2.0 — 2026-11-24

### 新增：历史会话（多会话）

- 新增 `dsh_store.py`：`index.json` 索引 + `sessions/<sessionId>.jsonl` 逐会话事件 + `trash/` 回收站，落盘在 `<ComfyUI>/user/default/dsh-agent/`。
- 引擎改为多会话：每个会话独立缓冲事件（按 `sessionId` 分桶），切换时用 SDK 的 `session/resume` 接续（历史不重放，由本地 store 回放给 UI）。
- 新路由：`GET /dsh/sessions`、`POST /dsh/session/{new,select,rename,trash,restore,purge}`；`/dsh/chat`、`/dsh/poll` 接受 `sessionId`。
- 前端：头部「历史会话 (N) ▾」——标题（首条消息自动截断 24 字，可改名）· 条数 · 相对时间，当前项 ●；底部 `回收站 (N) ›`，恢复 / 彻底删除 / 清空回收站。
- 重启 ComfyUI 后自动打开上次会话；多个会话可同时存在（切换时提示显存占用）。

### 新增：授权卡 / 询问卡

- `approval/request` 不再静默自动放行，改为卡片：工具名、callId 前 8 位、理由，三个按钮「允许一次 / 本会话总是允许 / 拒绝」。
- 5 分钟未处理按 **拒绝** 结束；同会话发新消息时，挂起的旧授权卡按 **拒绝** 收尾。
- `ask_user_question` 卡片在选项之外增加自定义输入（`custom`），支持只填答案不选选项。
- 新路由：`POST /dsh/approve`、`POST /dsh/autonomy`（本会话自动放行开关）；`/dsh/status` 增加 `autoApprove`。

### 修复：一轮对抗性代码审查发现的问题

- **critical** `_ready_event` 只 set 不 clear：runtime 崩过一次后，之后每次 `ensure_started()` 都秒抛「启动失败」；且不清理旧进程可能同时跑两个 runtime。→ 启动前 `clear()`，`_spawn_locked()` 先终止旧进程。
- **critical** 会话存储无锁 + 固定临时文件名：并发写会把 `index.json` 写坏，下次加载静默变空索引（历史"消失"）。→ `RLock` 全程保护、`mkstemp` 唯一临时名 + `fsync` + `os.replace`、索引损坏时备份并从 `sessions/*.jsonl` 重建。
- **high** `sessionId` 未校验直接拼路径（`../`、绝对路径可逃逸）→ 白名单 `^comfyui-[A-Za-z0-9_-]{1,64}$`。
- **high** 发新消息会把挂起授权自动 `allowed-once`（等于替你点了允许）→ 改为 `rejected`。
- **high** `/dsh/*` 无 Host/Origin 校验、`/dsh/approve` 缺 `outcome` 默认放行、`purge` 可越过回收站 → 19 个路由统一 loopback 守卫、`outcome` 必填、`purge` 必须带 id 或显式 `{"all": true}`。
- **medium** 轮询无在途去重且串会话、坏图不 ack 导致每 700ms 重投、卡片关闭选择器注入、菜单键盘不可达 → 一并修掉；`turn/start` 时重置 `turn_had_text`，`session/title` 截掉注入的 `<comfy_context>`。

### 验证

- `py_compile` ×4、`node --check`；会话接口全链路（new/select/rename/trash/restore/purge + 无 id purge → 400）。
- 真实对话：`user → status → title → assistant → status`；重启后历史仍可回放并继续对话。
- 守卫：`Origin: http://evil.example` → 403；裸 socket `Host: evil.example:8188` → 403，`Host: 127.0.0.1:8188` → 200。
- 授权卡 E2E（`plugin_manager` 触发）、本会话自动放行、询问卡自定义输入、无头 Edge + CDP 面板 DOM 断言（历史菜单与卡片按钮）。

## v0.1.0 — 2026-11-22

首个公开版本（Windows 实机验证 + macOS 路径保留）。

### 新增：Windows 支持

- `dsh_engine.py` 的 `_discover()` 重构为 `_candidate_pairs()`：
  - macOS：`/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness` 与 `.../Contents/Resources/app.asar/dsh/node_modules/@deepseek-ai/dsh`
  - Windows：`%LOCALAPPDATA%\Programs\DeepSeek Harness\DeepSeek Harness.exe` 与 `...\resources\app.asar\dsh\node_modules\@deepseek-ai\dsh`
  - 兜底：`ProgramFiles` / `ProgramW6432` / `ProgramFiles(x86)`
  - 只对 exe 做 `os.path.exists()`；asar 是单文件归档，内部路径无法 stat，`bin.js` 交给 Electron 读取
- 仍支持 `DSH_NODE_EXECUTABLE` / `DSH_INSTALL_DIR` 手动覆盖。

### 修复：Windows 上中文变 `?`

- 子进程环境追加 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`。
- `mcp_server.py` 的 `main()` 对 `sys.stdin` / `sys.stdout` 做 `reconfigure(encoding="utf-8", errors="replace")`，stdout 额外 `newline="\n"`。
- 现象：面板里 agent 的回复出现成片 `?`；排查时容易被误判为节点 bug。

### 修复：面板重复点开关后上下文消失

- 根因一：`buildPanel()` 第一行 `root.innerHTML = ""`，重开面板时把已渲染消息全部清掉。
- 根因二：重开时请求 `/dsh/poll?cursor=-1`，把游标设成「最新事件号」，关闭期间到达的回复被永久跳过。
- 修法：
  - 复用已渲染的 DOM 节点（`S.panelKids`），判据改为「子节点是否仍挂在目标 root 上」，不再无条件清空。
  - 轮询从 `S.cursor` 继续，不再重置到最新。
  - 重开时滚到底部；`startPolling` / `startGraphSync` 先清理旧定时器，避免重复轮询。
- 注意：这只影响可见历史；引擎侧会话一直在，不会因为开关面板而重建。

### 验证环境

- Windows 11 + ComfyUI 0.46.1（frontend 1.59.6）+ DeepSeek Harness Desktop 0.2.2-rc.5，AMD RX 7800XT（ROCm 7.14 / torch 2.12）。
- 验证项：节点加载、`/dsh/status`、面板渲染与截图、`POST /dsh/chat` 完整事件流（tool → tool_result → assistant）、中文往返、侧栏图标位置。
