# v0.2.0 补丁说明（历史会话 + 授权卡 / 询问卡）

本版在 v0.1.0（Windows 适配 + 面板保活）之上，加了两块用户能直接感知的东西，
并按一次 5 视角对抗性审查（安全 / 并发 / 前端 / 数据完整性 / Windows）修掉了
若干真实缺陷。

## 一、可持久化的历史会话

之前每次启动都会新开一个会话 id，关掉面板再打开就看不清刚才聊到哪。现在：

- 新增 `dsh_store.py`：`index.json` + `sessions/<sid>.jsonl` + `trash/`，
  全部落在 `<ComfyUI>/user/default/dsh-agent/`。不写 DSH 自己的目录。
- 面板头部「新对话」左边多了一个 **`历史会话 (N) ▾`**：
  标题 · 条数 · 相对时间；当前项带 ●；✎ 改名；🗑 移到回收站。
- 底部 `回收站 (N) ›`：恢复 / 彻底删除 / 清空回收站。主列表只能"移到回收站"，
  想彻底删必须进回收站再点一次——防手滑。
- 允许多个会话同时存在（SDK 的 agent 活到进程退出，这是它的天然形态）。
  切换时会弹一次提示：旧会话若还在跑任务会继续在后台跑，两个一起跑会抢显存，
  让用户自己判断要不要切。
- 重启 ComfyUI 后自动打开上次那个会话；历史从磁盘回放。
- 旧版本已经被 DSH 落盘的 `session.v4.jsonl.zstd` 可以导入：
  那文件是多帧 zstd 拼接，需要按 magic 逐帧解（见 `dsh_store._read_legacy_log`，
  复用 `work/unpack_v4.mjs`）。

## 二、授权卡与询问卡

**授权**（`approval/request`）：

- 从"一行字 activity"改成卡片：工具名 + callId 前 8 位 + reason +
  三个按钮「允许一次 / 本会话总是允许 / 拒绝」。
- 5 分钟不点，按 **拒绝** 处理（不是放行）。
- 同一个会话里用户又发了新消息：挂着的授权卡一律按 **拒绝** 结束，
  免得"再说一句话就等于批准"。
- 每个会话各自记住"总是允许"；`/dsh/autonomy` 可以显式开关，
  `/dsh/status` 回 `autoApprove`。

**询问**（`user-question/ask`）：

- 选项之外多一个自定义输入框（Enter 提交），回答可以是 `selected: []` +
  `custom: "..."`。
- 提交失败会提示"卡片可能已过期"，不会静默卡死。

## 三、审查后修掉的问题（摘要）

| 级别 | 位置 | 问题 | 处理 |
| --- | --- | --- | --- |
| critical | `dsh_engine.ensure_started` | `_ready_event` 只 set 不 clear，崩溃/重置后立刻误报启动失败 | 启动前 `clear()`；`_spawn_locked()` 先收掉旧进程 |
| critical | `dsh_store` | 无锁 + 固定 `.tmp` 名，并发写会写出半截 index → 历史全丢 | 全程 `RLock`，`mkstemp` + `fsync` + `os.replace`，损坏时备份并从 `sessions/` 重建 |
| high | `dsh_store` / 路由 | sessionId 直接拼路径，`../` 可逃出 root | `safe_sid()` 正则白名单，非法直接 ValueError |
| high | `_flush_pending` | 发新消息会把挂起的授权自动放行 | 改为一律拒绝 |
| high | `/dsh/*` | 无 Host/Origin 校验，任意网页可驱动 | 只允许 loopback Host + loopback Origin，否则 403 |
| high | `/dsh/approve` | 缺 outcome 时默认 allowed-once | outcome 必填且白名单 |
| high | `/dsh/session/purge` | 缺 sessionId 等同"清空回收站" | 必须显式 `all: true`；单个 id 必须已在回收站 |
| high | `_call` | runtime 退出后在途请求白等 30–120s | 退出时统一失败唤醒 |
| medium | 前端 | poll 无在途去重、切会话仍回写游标 | `pollInFlight` + 请求前后比对 `activeSession` |
| medium | 前端 | 坏图 ack 失败 → 每 700ms 重投刷屏 | 失败也 ack |
| medium | 前端 | `markCardClosed` 把 requestId 拼进选择器 | 改按 `dataset.req` 比对 |

## 四、验证记录

- `py_compile`（4 个 .py）、`node --check`（`dsh_agent.js`）通过。
- `/dsh/sessions`、`/dsh/session/{new,select,rename,trash,restore,purge}` 全链路通过；
  重启后历史仍能回放、继续对话。
- 真实对话（新会话）：`user → status → title → assistant("收到") → status`。
- 授权卡：`plugin_manager` 触发（reason 含 `escalate sandbox to danger-full-access`）；
  「允许一次」→ `approval_closed` → `tool_result` → assistant；
  「本会话总是允许」→ 后续只留一行 activity，不再弹卡。
- 询问卡：`ask_user_question` + 自定义输入 `紫罗兰` → assistant 正确复述。
- DOM 断言（无头 Edge + CDP）：侧栏项 aria-label `DeepSeek Harness Agent`，
  卡片渲染出三个按钮。
- Host 守卫：裸 socket 发 `Host: evil.example:8188` → 403。

## 五、仍然存在的限制

- SDK 会话策略在某些配置下等同于完全放行，`approval/request` 不会到达；
  卡片路径只在"需要提权/审批"的工具（如 `plugin_manager`）上能实测。
- 两个会话同时跑会争 GPU；面板只做提示，不做资源仲裁。
- 回收站没有自动过期清理。
