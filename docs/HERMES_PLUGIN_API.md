# Hermes 插件 API：真实签名

> **对照版本**：Hermes Agent **v0.22.6**（`git` 安装）。
> 本文只记录**实测确认过的接口**，不猜。写 `shim.py` / `adapter.py` 之前先读这里。
> 本文只保证「怎么接进 Hermes」这件事是对的；本插件的分层见 `README.md`。

---

## 1. 加载契约

Hermes 只调用插件包的 `register()`，每个进程一次。

```python
# __init__.py
from .shim import register
__all__ = ["register"]
```

```python
# shim.py
def register(ctx) -> None:
    ctx.register_platform(
        name="pi-workbench-gateway",      # 平台名，见 §4
        label="Pi Workbench Gateway",
        adapter_factory=_make_adapter,    # 惰性构造，gateway 真正启动该平台时才调用
        check_fn=_deps_ok,                # 可选：被动探测依赖是否可导入，**绝不能装东西**
        required_env=["PI_HUB_ADMIN_KEY"],# 可选：缺了就报告未就绪
        install_hint="在 gateway 服务的环境里设置 PI_HUB_ADMIN_KEY",
    )
```

要点：

- `register()` **不许在 import 期抛异常**。插件坏了要退化成一条 warning，不能把 gateway 主进程带下水。
- `check_fn` 会被 gateway 随意调用（用于状态展示），必须是纯被动探测。
- `adapter_factory(config)` 收到 Hermes 的 config，返回 adapter 实例；构造要惰性（在函数体里
  `from .adapter import ...`），这样模块 import 期不碰 `gateway.*`。

## 2. 持久化目录

```python
from plugins.plugin_storage import plugin_data_dir
data_dir = plugin_data_dir(PLUGIN_ID)   # → <HERMES_HOME>/plugin-data/<插件目录名>/
```

实测落点（部署机）：`/home/ubuntu/.hermes/profiles/pi-cloud/plugin-data/<name>/`。
SQLite、审计日志都放这里；**不要**写进 profile 的 `config.yaml` 或 `.env`。

## 3. Adapter 契约

继承自 Hermes 的 `gateway.platforms.base.BasePlatformAdapter`：

```python
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult

class Adapter(BasePlatformAdapter):
    def __init__(self, config, *, data_dir=None):
        super().__init__(config, Platform(PLATFORM_NAME))

    async def connect(self, *, is_reconnect: bool = False) -> bool: ...
    async def disconnect(self) -> None: ...

    # 聊天面：我们是 HTTP 入口，不接收入站聊天
    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult: ...
    async def get_chat_info(self, chat_id) -> dict: ...
```

三条硬约束：

1. **`connect()` 里起服务，`disconnect()` 里停服务**。gateway 负责生命周期，插件不要自己起线程
   在 import 期跑。
2. **端口被占用必须返回 `False`，不许抛**。抛出会让 gateway 认为整个平台崩了。
3. `send()` 返回 `SendResult(success=False, error=...)`——我们不承载对话投递。

## 4. `plugin.yaml` 真实字段

以下形状已实测可加载（实际值看仓库根的 `plugin.yaml`）：

```yaml
name: pi-workbench-gateway  # 必须与插件目录名一致（plugins/<name>/）
label: Pi Workbench Gateway
kind: platform
version: 0.1.1
manifest_version: 2
author: Pi Workbench
description: >
  ...多行描述...
requires_hermes: ">=0.22.6"
python_dependencies: []

requires_env:               # ← 字符串列表
  - PI_HUB_ADMIN_KEY

optional_env:               # ← 对象列表
  - name: HERMES_API_BASE
    description: "..."
    prompt: "..."
    password: false
```

容易写错的两处：

- `requires_env` 是**字符串列表**，`optional_env` 是**带 `name/description/prompt/password` 的对象列表**。
- `name` 决定了插件目录名，也就决定了 `plugin_data_dir()` 的落点。改名等于换数据目录。

**旧版设了 `requires_hermes` 上界（`<0.24`）**，应用仓库的 `CLOUD_AGENT.md` §5.2 口径是
**不设上界**（上游补丁版本不应阻止安装）。新插件按文档走，不设上界；这条是我们的刻意偏离。

## 5. 环境变量归属

| 变量 | 谁提供 | 用途 |
| --- | --- | --- |
| `API_SERVER_KEY` | Hermes（profile `.env`） | 转发 `/v1/*` 时的 Bearer |
| `HERMES_DASHBOARD_SESSION_TOKEN` | Hermes（可选） | 转发 dashboard 侧路 |
| `HERMES_HOME` | systemd unit / Hermes | 定位 profile，用于推导默认媒体根 |
| `PI_HUB_*` | `<profile>/.env`（安装时 `requires_env` 提示写入） | 插件自己的配置（监听、限流、媒体根、管理密钥） |

`config.py` 是唯一读 `os.environ` 的地方：`shim.py` / `adapter.py` 不直接读环境变量，
配置一律经 `config.py` 取值。

### 5.1 安装与密钥落盘（0.22.6 源码实测）

```text
plugins_cmd_install.py:175  _prompt_plugin_env_vars()   → 逐个提示 + save_env_value()
plugins_cmd.py:430          _missing_env_specs()        → get_env_value() 判"没设置"才提示
config.py                   get_env_value()             → os.environ 先，再 <profile>/.env
config.py                   save_env_value()             → 写 <profile>/.env
gateway/run.py:1575         load_hermes_dotenv()         → 启动时把 .env 装进 os.environ
```

结论：

- `hermes plugins install <git-url|owner/repo>` 克隆到 `<profile>/plugins/<仓库名>/`，**目录名取自仓库名**，
  所以仓库名必须等于清单里的 `name`。装完自动跑 `_prompt_plugin_env_vars`，答案写进 `<profile>/.env`。
- 提示只针对"`get_env_value()` 认为没设置"的变量：进程环境或 `.env` 里已有的不再问。
- `requires_env` 写纯字符串 → 普通 `input()`（可管道喂）；写 `{name, secret: true}` → 遮罩输入（要 TTY）。
  自动化友好度上两者有实质差别。
- `install` **不**自动跑校验：`validate` / `doctor --ci` 是独立步骤。
- `plugins remove` 不删 `plugin-data/<name>/`，数据目录要手动清。
- `.env` 由 Hermes 装载进 `os.environ`，所以插件只读进程环境即可，不要自己解析 `.env`。

## 6. CLI 可用命令（实测）

```text
hermes plugins {install, search, browse, validate, update, adopt,
                trust-update-url, check-updates, remove, list,
                enable, disable, capabilities, doctor, pack, show}
```

- `install <git-url> --no-enable`：先装不启用（缺 `requires_env` 时）。
- `disable <name>`：热卸载插件，但**已接上的平台钩子要重启 gateway 才真正释放**（CLI 自己会提示）。
- `remove <name>`：删插件目录；**不删** `plugin-data/`，要手动清。
- `list`：显示 name / status / version / description / source（`bundled` / `user`）。

## 7. 部署形态（部署机实测）

```text
HERMES_HOME   <profile 目录>（<profile>/.env 放 Hermes 自己的密钥与 PI_HUB_*）
hermes CLI    <hermes 安装目录>/.hermes/bin/hermes          (v0.22.6, git 安装)
unit          hermes-<profile>-gateway.service     → api_server 127.0.0.1:8642
              hermes-<profile>-dashboard.service   → dashboard 127.0.0.1:9119
Python        3.14.x（独立打包运行时；cron 表达式需要 croniter，见 CONTRACT）
```

远程端口若被网络策略拦住，先在本地起 SOCKS 隧道，再用 `ssh -o ProxyCommand` 过去的
方式不写进仓库（涉及具体主机与密钥路径）。

## 8. 后续若要扩展

1. `ctx.register_platform` 还有几个参数未用到（本插件只用必需集）。
2. `BasePlatformAdapter` 只实现了 `send` / `get_chat_info`，其余抽象方法未触及。
3. `plugin_data_dir()` 对未注册 id 的行为未验证。
4. `kind: platform` 之外是否有更贴合「纯 HTTP 入口」的类型（若有，可省掉 chat 面的两个空实现）。
