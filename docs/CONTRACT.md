# 跨仓库契约：桌面端 → 网关 → Hermes

> **来源**：应用仓库 `electron/cloud/*`（已实现并测试过的客户端），不是照方案文档抄的。
> **纪律**：应用侧每新增一个云端接口，都要同步这里；插件是 deny-by-default，白名单外的路径
> 一律 `404 not_found`。漏了不是 bug，是设计。
>
> 待补：每条路径的完整响应字段（下一步从 `remote.ts` 的 `Raw*` 类型逐个填）。

## 鉴权

| 客户端 | 头 |
| --- | --- |
| 桌面端 → 网关 | `Authorization: Bearer pi_...`（设备 token） |
| 网关 → api_server | `Authorization: Bearer <API_SERVER_KEY>`，并注入 `X-Pi-Device: <deviceId>` |
| 网关 → 管理接口 | `X-Pi-Admin-Key: <PI_HUB_ADMIN_KEY>`（只在服务器侧用） |

网关转发时必须**删掉**客户端的 `Authorization` / `X-Pi-Admin-Key` / `X-Pi-Device`，
再注入自己的。桌面端永远不传管理密钥。

## 设备与网关

| 方法 | 路径 | 鉴权 | 备注 |
| --- | --- | --- | --- |
| `GET` | `/v1/hub/health` | 无 | **瘦身版**：只回 `status` / `upstreamOk` / `hub` / `contractVersion` / `features` |
| `GET` | `/v1/hub/status` | Device 或 Admin | 完整诊断（设备计数、上游版本、dashboard 地址、运行时长） |
| `POST` | `/v1/devices/pairing` | Admin | 生成连接码，返回 `{code, expiresAt, pairingUrl}` |
| `POST` | `/v1/devices/pair` | 无 | body `{code, deviceId, name}` → `{token, tokenPrefix, deviceId, name, scopes, dashboardUrl}` |
| `GET` | `/v1/devices/me` | Device | 身份、scope、`lastSeenAt` / `online` / `onlineWindow` / `serverTime` |
| `POST` | `/v1/devices/me/heartbeat` | Device | 心跳，30 秒一拍 |
| `POST` | `/v1/devices/{id}/revoke` | Device 自身 / Admin | 自注销只切断自己 |
| `POST` | `/v1/devices/register` | Admin | break-glass，桌面端不调用 |

## 工作区派活（0.2.0 新增，0.3.0 补 `input_required` / 租约重投 / A2A）

设备侧要 `dispatch` scope（配对时固定签发）。云端 Hermes 走**进程内工具**，不经过这些 HTTP 端点；
这些端点是给桌面 worker 与诊断用的。

| 方法 | 路径 | 鉴权 | 备注 |
| --- | --- | --- | --- |
| `GET` | `/v1/hub/relay/stream` | Device(`dispatch`) | 常连 SSE。连上先补发该设备的 `queued` **与正在跑的 `running` / `input_required`**（断网重连要能接回同一任务），之后每 25s 一条 `: ping`，最长 3600s 后发 `relay.reconnect` 让客户端重连 |
| `POST` | `/v1/hub/relay/report` | Device(`dispatch`) | body `{taskId, kind, ...}`；`kind` ∈ `claimed` / `progress` / `question` / `done` / `failed` / `canceled`；只能报自己的任务（别人的任务回 404） |
| `POST` | `/v1/hub/workspaces` | Device(`dispatch`) | 全量上报工作区清单 `{dispatchable, workspaces:[{id,label,description}]}`；**整表替换**，少报即消失 |
| `GET` | `/v1/hub/dispatch/workspaces` | Device 或 Admin | 目录快照：`workspaceId` / `label` / `description` / `dispatchable` / `online` / `relayStreams` / `running` |

中继帧（`event:` 与 `data.type` 同值）：

| 帧 | 方向 | 载荷 |
| --- | --- | --- |
| `task.assigned` | hub → 设备 | `taskId` / `prompt` / `deviceId` / `workspaceId` / `contextId` / `deadlineAt` / `attempt` |
| `task.answered` | hub → 设备 | `taskId` / `answer`（本地反问的答案） |
| `task.cancel` | hub → 设备 | `taskId` / `reason` |
| `relay.reconnect` | hub → 设备 | 无（连接寿命到点，客户端应立即重连） |
| `: ping` | hub → 设备 | 注释帧，保活 |

回报的 `done` 可带 `summary` / `files`（相对路径）/ `usage` / `localSessionId`；`failed` 带 `error`；
`question` 带 `question`。终态任务的后续回报**不报错**，只回 `{accepted:false, state}`。

### P2 语义（0.3.0）

- **`input_required` 不是终态**：设备报 `question` 之后任务停在 `input_required`；云端
  `local_task_reply` → store 回到 `running` 并推 `task.answered`，本地同一轮继续。
- **租约**：`running` / `input_required` 任务 120s 没有任何回报（`progress` 也算回报）就重投，
  `attempt` 在 claim 时自增，上限 2 次；用尽后判 `failed`。后台每 15s 扫一次。
- **重连补发**：设备重连时同时补 `queued` + `running` + `input_required`，桌面 worker 按
  `taskId` 去重（正在跑的不重复起，等回答的继续等）。

## 对外 A2A（0.3.0 新增）

| 方法 | 路径 | 鉴权 | 备注 |
| --- | --- | --- | --- |
| `GET` | `/.well-known/agent-card.json` | Device(`dispatch`) 或 Admin | 把**可派活**工作区投影成 skills（`id` = `设备/工作区`，description 保留用户写的触发词） |
| `POST` | `/a2a` | Device(`dispatch`) 或 Admin | JSON-RPC 2.0 信封，方法见下 |

| JSON-RPC 方法 | 参数 | 返回 |
| --- | --- | --- |
| `message/send` | `message{role,messageId,parts:[{kind:"text",text}],contextId?}` + `skill`（`设备/工作区` 或工作区名）+ `configuration{blocking?,waitSeconds?,deadlineSeconds?}` | A2A `Task`；`messageId` 兼作幂等键 |
| `tasks/get` | `{id}` | A2A `Task` |
| `tasks/cancel` | `{id, reason?}` | A2A `Task`（已终态时原样返回，不报错） |
| `tasks/resubscribe` | `{id}` | SSE：`kind:"status-update"` 帧，终态后结束 |

状态映射：内部 `queued → submitted` / `running → working` / `input_required → input-required` /
`completed|failed|canceled` 同名 / `expired → failed`（A2A 没有 expired）。
`metadata` 里带 `deviceId` / `workspaceId` / `filesChanged` / `usage` / `localSessionId` / `attempt`。

## 上游白名单（`/v1/*`、`/api/*` 转发到 api_server）

| 方法 | 路径 |
| --- | --- |
| `GET` | `/v1/capabilities` |
| `GET` | `/health/detailed` |
| `GET` | `/v1/toolsets` · `/v1/skills` · `/v1/models` |
| `POST` | `/v1/runs`（body `{input, author, session_id?}` + `Idempotency-Key`） |
| `GET` | `/v1/runs/{id}` |
| `GET` | `/v1/runs/{id}/events`（SSE，续传参数是 **query `last_seq`**，不是 `Last-Event-ID` 头） |
| `POST` | `/v1/runs/{id}/stop` · `/steer`（body `{input}`）· `/resolve-unknown` |
| `POST` | `/v1/runs/{id}/approval`（body **三字段必填**：`request_id` + `execution_generation` + `choice`） |
| `POST` | `/v1/runs/{id}/clarify` |
| `GET` | `/api/sessions?limit&offset` · `/api/sessions/{id}` · `/api/sessions/{id}/messages?...` |
| `POST` | `/api/sessions` |
| `PATCH` | `/api/sessions/{id}` |
| `DELETE` | `/api/sessions/{id}` |
| `GET` | `/api/jobs?include_disabled=true` · `/api/jobs/{id}` |
| `POST` | `/api/jobs/{id}/run` · `/pause` · `/resume`（body `{}`） |
| `PATCH` | `/api/jobs/{id}`（只许 `name` / `schedule` / `prompt` / `enabled` 等上游白名单字段） |
| `DELETE` | `/api/jobs/{id}` |

## dashboard 侧路（不走 `/v1` 代理，单独处理）

| 方法 | 路径 | 处理 |
| --- | --- | --- |
| `GET` | `/api/config` | 只回白名单子集（`approvals.mode` / `approvals.timeout` / `agent.disabled_toolsets` / `terminal.backend` / `policy.denyCount`），**绝不回整份配置** |
| `PUT` | `/api/config` | 只接受 `{"config":{"approvals":{"mode":"manual|smart|off"}}}`；写后读回复核 |
| `POST` | `/api/chat/image-upload` | body `{data_url, filename}` |
| `GET` | `/api/fs/read-data-url?path=` | `fsguard` + `realpath` 白名单 |

真机验收踩过的两个坑（别再踩）：

- 上传是 **JSON**（`data_url` 是完整 data URL 字符串），**不是 multipart**。用 `curl -F` 会掉进上游
  FastAPI 的 422 编码器崩溃（`UnicodeDecodeError` → `500 Internal Server Error`），症状是
  「插件 500 了」，其实是请求形状错。
- 越界路径（包括 profile 的 `.env` 本身）一律 `403 path_not_allowed`，不是 404。

## 上游依赖（定时任务）

`POST /api/jobs` 透传给 api_server，调度表达式由 Hermes 的 `cron/jobs.py::parse_schedule` 解析：

- 间隔 / 一次性（`30m`、`every 2h`、`in 30m`、ISO 时间）：纯标准库，装哪个运行时都能用。
- cron 表达式（`0 9 * * *`）与自然语言日期（`every monday 9am`、`weekdays at 9am`）：要 `croniter`。
  Hermes 在 `pyproject.toml` 里把它声明为核心依赖，但独立打包的运行时**不一定预装**；
  缺的时候上游直接报错。补法：用跑 gateway 的那个解释器 `-m pip install "croniter==6.2.4"`。
  （注意 `hermes update` 可能重置运行时，升级后重新确认一次。）

## 错误形状（桌面端按 code 分支）

```json
{ "error": { "code": "unauthorized", "message": "...", "type": "hub_error" } }
```

`type` 字段旧插件是 `hub_error`，桌面端目前只读 `code` / HTTP 状态码；新插件保持一致。
完整 code 表见应用仓库 `CLOUD_AGENT.md` §4.4。

## 桌面端依赖的两个细节（踩过）

1. `POST /v1/runs/{id}/approval` 少任何一个字段都是 `400 invalid_params`（Hermes 0.22.6 实测）。
2. SSE 必须逐块到达：`X-Accel-Buffering: no` + Caddy `flush_interval -1`，插件侧**逐行读上游**
   （`read(n)` 会阻塞到 n 字节或 EOF，那正是「对话卡住、最后一次性出全文」），否则表现为
   「对话卡住、最后一次性出全文」。
3. `/v1/hub/health` 已瘦身（旧版对匿名访客吐出设备计数、内部端口、运行时长）。桌面端要看完整
   诊断请打 `/v1/hub/status`（设备 token 即可）。
