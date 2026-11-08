# Pi Workbench Gateway

Pi Workbench 桌面端与云端 Hermes Agent 之间**唯一的一条缝**。

桌面端不直连云端 Hermes：所有会话、审批、任务、媒体请求都打到本插件监听的
`127.0.0.1:8484`，由它完成设备鉴权、限流、审计，再转发给本机的 Hermes
`api_server`（`127.0.0.1:8642`）。除此之外只有 dashboard 由桌面端直接打开。

```text
Pi Workbench ──HTTPS + 设备 token──▶ Caddy ──▶ 本插件 (127.0.0.1:8484) ──▶ Hermes api_server (127.0.0.1:8642)
```

## 状态

**0.3.2**：云端 Agent 主链路（配对 / 会话 / 审批 / 任务 / 媒体 / 心跳 / 审计）+
**工作区派活**（`tools.py` 的 `local_*` 工具 + `hub/dispatch.py` 任务 store +
`hub/relay.py` 设备中继）。0.3.0 补齐派活 P2/P3：本地反问（`input_required`）、
租约到期重投、重连接回同一任务、后台维护线程（超时收尾 / 7 天清理），以及对外
A2A 端点（`/.well-known/agent-card.json` + `POST /a2a`）。0.3.1 增加**轨迹可查**，0.3.2 修正「设备忙」提示
（`local_task_trace` / `tasks/get` + `include:["trace"]`：默认不推送，云端要看得自己拉）。
当前进度、协议、验收标准见应用仓库的
`docs/requirements/CLOUD_AGENT.md` 与 `docs/requirements/WORKSPACE_DISPATCH.md`（唯一事实源）。

## 硬约束

- 只依赖 **Python 标准库**（`http.server` / `http.client` / `sqlite3` / `threading` / `secrets`）。不引入
  FastAPI、aiohttp、uvicorn；`python_dependencies: []`。
- 只有 `shim.py` / `adapter.py` 接触 Hermes 插件 API；只有 `config.py` 读 `os.environ`。
- `hub/` 完全不知道 Hermes 存在：它只认「上游是一个 HTTP 服务」。因此整套逻辑可以在没有
  Hermes 的机器上跑起来并测试。
- 插件异常不许打穿 Hermes gateway 主进程：服务跑在 daemon 线程。

## 本机开发与测试

不需要 Hermes，也不需要 Linux：

```bash
python -m unittest discover -s tests -t . -v     # 全量测试（零第三方依赖）
python -m hub.devserver                          # 本机起假上游 + 网关，供手工验证
```

## 安装（部署到 Hermes）

走 Hermes 官方安装流程，不要手拷文件：目录名取自仓库名，必须与清单里的 `name` 一致。

```bash
# 装完会提示填写 PI_HUB_ADMIN_KEY，答案由 Hermes 写进 <profile>/.env 并自动启用
hermes plugins install https://github.com/T-Chen-CN/pi-workbench-gateway.git --yes-deps --enable

hermes plugins validate pi-workbench-gateway      # 清单自检
hermes plugins doctor pi-workbench-gateway --ci   # 有错就非零退出
```

非交互（自动化 / CI / 云主机初始化）——`requires_env` 声明成纯字符串，提示读 stdin，
所以可以把值管子进去：

```bash
printf '%s\n' "$PI_HUB_ADMIN_KEY" | hermes plugins install \
  https://github.com/T-Chen-CN/pi-workbench-gateway.git --yes-deps --enable
```

- 密钥落盘位置是 `<profile>/.env`（Hermes 官方约定，和 `API_SERVER_KEY` 同处）；
  gateway 启动时把 `.env` 装载进 `os.environ`，插件只读进程环境。
- 想要"先装不启用"就用 `--no-enable`，之后 `hermes plugins enable pi-workbench-gateway`。
- **`hermes plugins remove` 不会删 `<profile>/plugin-data/pi-workbench-gateway/`**（设备库、审计
  在那儿），要彻底清干净得手动删。

部署拓扑、Caddy、连接码生成与验收，见应用仓库
`docs/requirements/CLOUD_AGENT.md` §7。

## 目录

```text
plugin.yaml          Hermes manifest（kind: platform）
__init__.py          register(ctx) → shim
shim.py              唯一接触 Hermes 插件 API（保持极薄）
adapter.py           启停
config.py            唯一读环境变量的地方
hub/                 纯标准库 HTTP 服务，不知道 Hermes 存在
  server.py          ThreadingHTTPServer + 生命周期
  router.py errors.py auth.py db.py audit.py
  pairing.py ratelimit.py upstream.py fsguard.py
  routes/            health / pairing / devices / proxy / policy / dispatch（中继流 + 上报 + 目录）
  dispatch.py        派活任务 store（状态机 / 幂等 / claim / 租约 / 事件）
  relay.py           每设备中继扇出（queue.Queue，满则丢并在重连后补发）
  devserver.py       本机开发入口（假上游）
tools.py             Hermes 侧派活工具（toolset `pi_workbench`，只在 shim 里注册）
tests/               单元 + 集成 + 契约，全部标准库
docs/
  HERMES_PLUGIN_API.md   Hermes 插件 API 的真实签名（写代码前先读这篇）
  CONTRACT.md            桌面端实际发出的请求形状（跨仓库契约）
```

## 契约纪律

`docs/CONTRACT.md` 是**跨仓库契约**：应用侧每新增一个云端接口，都必须同步更新它，
否则插件会以 `404 not_found` 拒绝——这是 deny-by-default 的刻意设计，不是疏漏。

## 许可

MIT
