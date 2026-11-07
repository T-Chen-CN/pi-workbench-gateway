# Pi Workbench Gateway

Pi Workbench 桌面端与云端 Hermes Agent 之间**唯一的一条缝**。

桌面端不直连云端 Hermes：所有会话、审批、任务、媒体请求都打到本插件监听的
`127.0.0.1:8484`，由它完成设备鉴权、限流、审计，再转发给本机的 Hermes
`api_server`（`127.0.0.1:8642`）。除此之外只有 dashboard 由桌面端直接打开。

```text
Pi Workbench ──HTTPS + 设备 token──▶ Caddy ──▶ 本插件 (127.0.0.1:8484) ──▶ Hermes api_server (127.0.0.1:8642)
```

## 状态

**开发中，尚未发布可用版本。** 当前进度、协议、验收标准见应用仓库的
`docs/requirements/CLOUD_AGENT.md`（唯一事实源）。

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

```bash
hermes plugins install https://github.com/T-Chen-CN/pi-workbench-gateway.git --no-enable
# 配好 PI_HUB_ADMIN_KEY 等环境变量后再启用
hermes plugins enable pi-workbench-gateway
```

部署拓扑、Caddy、systemd drop-in、连接码生成与验收，见应用仓库
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
  routes/            health / pairing / devices / proxy / policy
  devserver.py       本机开发入口（假上游）
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
