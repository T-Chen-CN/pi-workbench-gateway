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
| 网关 → api_server | `Authorization: Bearer <API_SERVER_KEY>` |
| 网关 → 管理接口 | `X-Pi-Admin-Key: <PI_HUB_ADMIN_KEY>`（只在服务器侧用） |

网关转发时必须**删掉**客户端的 `Authorization` / `X-Pi-Admin-Key` / `X-Pi-Device`，
再注入自己的。桌面端永远不传管理密钥。

## 设备与网关

| 方法 | 路径 | 鉴权 | 备注 |
| --- | --- | --- | --- |
| `GET` | `/v1/hub/health` | 无 | 桌面端用它判断「网关活着 / 上游活着」 |
| `POST` | `/v1/devices/pairing` | Admin | 生成连接码，返回 `{code, expiresAt, pairingUrl}` |
| `POST` | `/v1/devices/pair` | 无 | body `{code, deviceId, name}` → `{token, tokenPrefix, deviceId, name, scopes, dashboardUrl}` |
| `GET` | `/v1/devices/me` | Device | 身份、scope、`lastSeenAt` / `online` / `onlineWindow` / `serverTime` |
| `POST` | `/v1/devices/me/heartbeat` | Device | 心跳，30 秒一拍 |
| `POST` | `/v1/devices/{id}/revoke` | Device 自身 / Admin | 自注销只切断自己 |
| `POST` | `/v1/devices/register` | Admin | break-glass，桌面端不调用 |

## 上游白名单（`/v1/*`、`/api/*` 转发到 api_server）

| 方法 | 路径 |
| --- | --- |
| `GET` | `/v1/capabilities` |
| `GET` | `/health/detailed` |
| `GET` | `/v1/toolsets` · `/v1/skills` · `/v1/models` |
| `POST` | `/v1/runs`（body `{input, author, session_id?}` + `Idempotency-Key`） |
| `GET` | `/v1/runs/{id}` |
| `GET` | `/v1/runs/{id}/events`（SSE，带 `Last-Event-ID`） |
| `POST` | `/v1/runs/{id}/stop` · `/steer`（body `{input}`）· `/resolve-unknown` |
| `POST` | `/v1/runs/{id}/approval`（body **三字段必填**：`request_id` + `execution_generation` + `choice`） |
| `POST` | `/v1/runs/{id}/clarify` |
| `GET` | `/api/sessions?limit&offset` · `/api/sessions/{id}` · `/api/sessions/{id}/messages?...` |
| `POST` | `/api/sessions` · `/api/sessions/{id}/fork` |
| `PATCH` | `/api/sessions/{id}` |
| `DELETE` | `/api/sessions/{id}` |
| `GET` | `/api/jobs?include_disabled=true` · `/api/jobs/{id}` |
| `POST` | `/api/jobs/{id}/run` · `/pause` · `/resume`（body `{}`） |

## dashboard 侧路（不走 `/v1` 代理，单独处理）

| 方法 | 路径 | 处理 |
| --- | --- | --- |
| `GET` | `/api/config` | 只回白名单子集（`approvals.mode` / `approvals.timeout` / `agent.disabled_toolsets` / `terminal.backend` / `policy.denyCount`），**绝不回整份配置** |
| `PUT` | `/api/config` | 只接受 `{"config":{"approvals":{"mode":"manual|smart|off"}}}`；写后读回复核 |
| `POST` | `/api/chat/image-upload` | body `{data_url, filename}` |
| `GET` | `/api/fs/read-data-url?path=` | `fsguard` + `realpath` 白名单 |

## 错误形状（桌面端按 code 分支）

```json
{ "error": { "code": "unauthorized", "message": "...", "type": "hub_error" } }
```

`type` 字段旧插件是 `hub_error`，桌面端目前只读 `code` / HTTP 状态码；新插件保持一致。
完整 code 表见应用仓库 `CLOUD_AGENT.md` §4.4。

## 桌面端依赖的两个细节（踩过）

1. `POST /v1/runs/{id}/approval` 少任何一个字段都是 `400 invalid_params`（Hermes 0.22.6 实测）。
2. SSE 必须逐块到达：`X-Accel-Buffering: no` + Caddy `flush_interval -1`，否则表现为
   「对话卡住、最后一次性出全文」。
