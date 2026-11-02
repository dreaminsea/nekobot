# nekobot

一只住在 QQ 群和飞书里的猫娘机器人喵~

nekobot 是一个**可拓展的多平台 IM 机器人**：通过 adapter 层同时连接 QQ（经 NapCat / OneBot 11）
和飞书 / Lark，把不同平台的消息统一成平台无关的事件，再交给大模型（默认 DeepSeek `deepseek-flash`）
理解和回复。它能看图、读合并转发、上网搜索、记住事情、自己收藏表情包，在群里会判断「这句话该不该接」，
主人还能在聊天里远程更新、重启和备份它，不用登录服务器。

---

## 目录

- [功能一览](#功能一览)
- [快速开始](#快速开始)
- [平台接入](#平台接入)
- [配置说明](#配置说明)
- [日常使用](#日常使用)
- [功能详解](#功能详解)
- [运维](#运维)
- [数据与备份](#数据与备份)
- [开发与扩展](#开发与扩展)
- [常见问题](#常见问题)
- [安全与隐私](#安全与隐私)

---

## 功能一览

| 类别 | 能力 |
| --- | --- |
| 多平台 | QQ（NapCat）与飞书可**同时并发运行**，互相隔离：一个平台断线或未配置不影响另一个 |
| 对话 | 私聊直接聊；群聊 @、引用、戳一戳或触发前缀即回应；默认短答，必要时拆成多个消息气泡 |
| 群聊判断 | 消息合批处理、未直接点名的消息先做「要不要接话」快速判断、生成期间重复提问自动查重、一次可发多条并分别引用 |
| 多模态 | 图片直接进入主对话（原生图文输入），可回看旧图；表情包自动识别、注释、收藏和发送 |
| 工具调用 | 联网搜索、长期记忆、通讯录、跨平台发消息、联系主人、戳一戳、读取合并转发、请求后台活动等 |
| 共享经历 | 群聊、私聊和后台活动共享一份近期经历，bot 知道「别处刚发生了什么」 |
| 后台活动 | 外层事件循环管理聊天之外的持续任务，内置「定时提醒」 |
| 远程运维 | 主人私聊 `/update`、`/restart`、`/reload`、`/git`、`/backup`、`/napcat` 等，热重载失败自动回滚 |
| 稳定性 | QQ 掉线监控（飞书告警 + 自动重启 NapCat + 推送登录二维码）、重启后通知主人 |
| 数据 | 聊天历史、记忆、表情库本地持久化；一条命令在线/离线全量备份与恢复 |
| 可扩展 | 插件、中间件、LLM 工具、LLM 供应商、平台 adapter、后台活动六种扩展点，均自动发现注册 |

---

## 快速开始

### 1. 准备环境

- Linux / macOS，已安装 **Git**。
- [**uv**](https://docs.astral.sh/uv/)：管理 Python 与依赖。项目使用 Python **3.12**（见 `.python-version`），uv 会自动下载。
- 一个 **DeepSeek API Key**（或其他 OpenAI 兼容接口的 Key，见[接入其他模型](#3-接入新的-llm-供应商)）。
- 至少一个平台：
  - QQ：一个跑着 [NapCat](https://napneko.github.io/) 的 QQ 号；
  - 飞书：一个在[飞书开放平台](https://open.feishu.cn/)创建的企业自建应用。

### 2. 获取代码并安装依赖

```bash
git clone https://github.com/dreaminsea/nekobot.git
```

```bash
cd nekobot && uv sync --locked
```

uv 会在项目目录下创建 `.venv` 并安装 `uv.lock` 中锁定的精确版本。

### 3. 填写密钥与账号（`.env`）

密钥、token 和主人 / 群号等个人信息**只写在 `.env`**，不写进 `config.py`。`.env` 已在 `.gitignore` 中，不会被提交。

```bash
cp .env.example .env && chmod 600 .env
```

然后编辑 `.env`（或者直接运行 `python3 configure.py env` 按提示逐项填写，见[命令行配置工具](#命令行配置工具configurepy)），至少填写：

```ini
LLM_API_KEY=sk-xxxxxxxx      # 模型 API Key
NAPCAT_TOKEN=xxxxxxxx        # 用 QQ 时：与 NapCat 里设置的 token 一致
FEISHU_APP_ID=cli_xxxxxxxx   # 用飞书时
FEISHU_APP_SECRET=xxxxxxxx   # 用飞书时
MASTER_NAME=主人昵称         # 写进人设提示词
MASTER_QQ=10001              # 主人 QQ（运维命令、联系主人都靠它）
MASTER_FEISHU=ou_xxxxxxxx    # 主人飞书 open_id
```

全部可用的键见 [`.env` 键一览](#env-键一览)。

### 4. 调整行为配置（`config.local.py`）

`config.py` 里是所有行为配置的**默认值**，随代码一起更新，不要直接改。要改哪项，就在项目根目录新建
`config.local.py`（已在 `.gitignore` 中），只写要改的项：

```python
PLATFORMS = ["onebot"]              # 只用 QQ
GROUP_TRIGGER = ["bot", "/neko"]
BOT_NAMES = BOT_NAMES + ["小喵"]    # 可以引用 config.py 里的默认值
```

第一次运行通常只需要看这几项：

- `PLATFORMS`：要启用的平台，默认 `["onebot", "feishu"]`，只用一个就只留一个。
- `SYSTEM_PROMPT`：人设。
- `GROUP_TRIGGER`：群聊触发前缀，默认 `bot`、`/chat`。
- `BOT_NAMES`：群友对 bot 的常用称呼，帮助模型判断「是不是在叫你」。

这些也可以用 `python3 configure.py config` 在菜单里修改（会自动写进 `config.local.py`）；
`python3 configure.py show` 会列出当前配置并提示漏填的项。

### 5. 启动

```bash
uv run --locked bot.py
```

看到 `启动平台适配器: onebot` / `启动平台适配器: feishu` 后，私聊 bot 说一句话试试。
主人可以私聊发 `/probe` 检查 QQ 链路是否正常。

> 同一目录只能运行一个 bot 实例：启动时会先获取进程锁（`.nekobot.lock`），重复启动会直接退出并提示正在运行。

---

## 平台接入

### QQ（NapCat / OneBot 11）

bot 作为 WebSocket **客户端**主动连接 NapCat 的正向 WebSocket 服务。

1. 部署 NapCat 并登录 bot 的 QQ 号。
2. 打开 NapCat WebUI → **网络配置** → **新建** → **WebSocket 服务端**：
   - 端口 `3001`（与 `config.NAPCAT_WS_URL` 一致，默认 `ws://127.0.0.1:3001`）；
   - 启用；
   - token 填一个随机字符串，并把同样的值写进 `.env` 的 `NAPCAT_TOKEN`。
3. `.env` 填 `BOT_QQ`（bot 自己的 QQ 号）。不填也能聊天——会从收到的消息里自动获取，但 NapCat
   重启命令和日志路径由它生成，不填就无法远程重启 NapCat。
4. `config.PLATFORMS` 包含 `"onebot"`。

部分功能依赖 NapCat 的扩展能力：

- **群聊戳一戳**需要 NapCat 的 Packet 发包能力；合并转发读取在常规获取失败时也会走 Packet 回退。
  主人私聊 `/probe` 可以看到 NapCat 版本和 `nc_get_packet_status`。该状态只证明基础 Packet 连接，
  不代表发包一定成功。
- **掉线监控 / 远程重启**假设使用 NapCat 安装脚本提供的 `napcat` 命令和默认路径，路径不同请在
  `config.local.py` 里设置 `NAPCAT_RESTART_CMD`、`NAPCAT_LOG_PATH`、`NAPCAT_QR_PATH`。

### 飞书 / Lark

bot 使用 lark-oapi 的**长连接**模式接收事件，不需要公网地址或回调 URL。

1. 在[飞书开放平台](https://open.feishu.cn/)（国际版为 Lark Developer）创建**企业自建应用**，开启**机器人**能力。
2. 在「凭证与基础信息」拿到 App ID 和 App Secret，写进 `.env` 的 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`。
3. 「事件与回调」→ 订阅方式选**使用长连接接收事件**，添加事件 `im.message.receive_v1`（接收消息）。
4. 「权限管理」中申请收发消息和图片相关权限，例如：
   - 读取用户发给机器人的单聊消息；
   - 接收群聊中 @ 机器人消息事件；
   - 获取群组中所有消息（想让 bot 在飞书群里潜水接话时需要，属于敏感权限）；
   - 以应用的身份发消息；
   - 获取与上传图片或文件资源。

   权限名称以开发者后台为准。
5. 发布应用版本，把机器人拉进群。
6. 国际版 Lark 把 `config.FEISHU_DOMAIN` 改为 `"lark"`。
7. 主人 open_id：先私聊 bot 一句话，在日志中找 `[私聊] (ou_xxx)`，把 `ou_xxx` 写进 `.env` 的 `MASTER_FEISHU`。
   非主人使用主人命令时，bot 也会回复对方的 id。

飞书侧已知限制：

- 接收事件不含发送者昵称，昵称会退化为 open_id。
- 没有「获取群组中所有消息」权限时，飞书群里只能收到 @ 机器人的消息，潜水 / 跟进接话会受限。
- 支持图片消息和富文本（post）中的图片；飞书自定义表情（sticker）消息暂不处理。
- 排查收不到消息时，把 `config.FEISHU_LOG_LEVEL` 设为 `"DEBUG"`。

### 只用一个平台

在 `config.local.py` 里写 `PLATFORMS = ["onebot"]` 或 `PLATFORMS = ["feishu"]` 即可，另一平台的配置留空不影响启动。
注意 [QQ 掉线监控](#qq-掉线监控) 需要两个平台同时启用（用飞书报告 QQ 掉线）。

---

## 配置说明

配置分三处，自己改的只有前两个，都不进 Git，所以 `/update` 拉取代码不会和本地配置冲突：

| 文件 | 放什么 | 是否提交到 Git |
| --- | --- | --- |
| `.env` | 密钥、token、主人 / 管理员 / bot 账号、群号 | **否**（已在 `.gitignore`） |
| `config.local.py` | 你改过的行为配置，只写和默认值不同的项 | **否**（已在 `.gitignore`） |
| `config.py` | 所有行为配置的默认值：平台、模型参数、提示词、阈值、开关 | 是，随代码更新 |

`config.local.py` 的规则：

- 它是普通的 Python 文件，在 `config.py` 默认值的基础上执行，所以可以引用默认值、`env()` 和其他配置，
  例如 `BOT_NAMES = BOT_NAMES + ["小喵"]`；顶层赋值的大写名字就是要覆盖的配置项。
- 依赖被改项的默认值会自动重新计算：例如改了 `LLM_MODEL`，`AMBIENT_REPLY_MODEL` 也跟着变；
  把 `LLM_BASE_URL` 换成其他供应商，联网搜索就不再复用 `LLM_API_KEY`。单独写了的项以你写的为准。
- 写了 `config.py` 里没有的名字（比如拼错）也会生效，但启动时会在 stderr 提示，`configure.py show` 也会提醒。
- 想改人设等多行提示词：把 `config.py` 里的整段赋值复制过来再改（`configure.py config` 里输入名字可自动复制）。
  复制后这一项就不再跟随 `config.py` 的更新。
- 密钥和账号仍写在 `.env`，不要写进 `config.local.py`。

`.env` 的加载规则：

- `.env` 位于项目根目录，与启动时所在的目录无关。格式为常见的 dotenv：`KEY=VALUE`，`#` 开头是注释，
  支持 `export ` 前缀和单 / 双引号；未加引号的值里，空白后的 ` #` 视为行内注释。
- **同名进程环境变量优先于 `.env`**，便于 systemd / Docker 注入。
- `.env` 的值只解析给 `config.py` 使用，不写入进程环境，bot 运行的外部命令（如 NapCat 重启）不会继承这些密钥。
- 格式错误的行会让启动失败并报告行号（不回显内容）。
- 改了 `.env`、`config.local.py` 或 `config.py` 都需要 `/restart`（或重启进程），`/reload` 不会重新读取。

### 命令行配置工具（`configure.py`）

不想手动编辑文件时，可以用自带的交互式配置工具。它只依赖 Python 标准库，系统自带的 `python3`（3.9 及以上）就能运行，不需要先 `uv sync`：

```bash
python3 configure.py            # 菜单：.env / 行为配置 / 查看并检查
python3 configure.py env        # 逐项填写 .env；不存在时从 .env.example 创建（权限 0600）
python3 configure.py config     # 修改行为配置的常用项，菜单里输入 a 切换到全部配置
python3 configure.py show       # 查看当前配置（密钥打码、标出本地改过的项），并检查漏填、拼写错误等常见问题
```

- 填 `.env` 时回车保留当前值，输入 `-` 清空；密钥输入不回显。未启用平台（`PLATFORMS`）的项会跳过，`env --all` 全部询问。
- 行为配置的改动只写进 `config.local.py`，**从不修改 `config.py`**；改回默认值（或在菜单里输入 `!`）会删掉对应的本地覆盖。
  布尔值输入 `y` / `n`，列表用逗号分隔，数字可以写 `8 * 1024 * 1024` 这样的算式。
- 两个文件都用语法树读写，不会执行；替换时只改值本身，注释保持不变。`SYSTEM_PROMPT` 等多行提示词没法逐项输入，
  在菜单里输入名字可以把默认写法复制到 `config.local.py`，再用编辑器修改。
- 写入前会重新解析校验，再原子替换文件。改完照常需要 `/restart`。

也可以非交互地修改，工具会自动判断写进 `.env` 还是 `config.local.py`，适合脚本或部署时使用：

```bash
python3 configure.py set PLATFORMS=onebot MASTER_QQ=10001 LLM_MODEL=deepseek-chat
python3 configure.py unset PLATFORMS DEEPSEEK_SEARCH_API_KEY   # 删除本地覆盖 / .env 的键，恢复默认
```

密钥写在命令行里会留在 shell 历史中，建议用 `env` 交互填写。

### `.env` 键一览

| 键 | 用途 | 说明 |
| --- | --- | --- |
| `LLM_API_KEY` | 模型 API Key | 必填；缺失时启动会警告，模型请求会失败 |
| `DEEPSEEK_SEARCH_API_KEY` | 联网搜索用的 DeepSeek 官方 Key | 可选。不写时：`LLM_BASE_URL` 是 DeepSeek 官方地址就复用 `LLM_API_KEY`，否则不启用搜索。写成空值 `DEEPSEEK_SEARCH_API_KEY=` 可单独关闭搜索 |
| `NAPCAT_TOKEN` | NapCat WebSocket 鉴权 token | 与 NapCat 中设置的一致；NapCat 未设 token 可留空 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书应用凭证 | 启用飞书时必填 |
| `MASTER_NAME` | 主人昵称 | 写进人设提示词，告诉模型主人叫什么 |
| `MASTER_QQ` | 主人 QQ 号 | 主人命令、`contact_master`、重启通知都依赖它；也会写进人设提示词，模型靠它在时间线里认出主人 |
| `BOT_QQ` | bot 自己的 QQ 号 | NapCat 重启命令与日志路径由它生成；掉线监控优先使用它 |
| `ADMIN_QQ` | QQ 管理员列表 | 逗号分隔；仅对声明了 `admin_only=True` 的插件生效 |
| `MASTER_FEISHU` | 主人飞书 open_id | 飞书侧主人命令、掉线告警依赖它 |
| `ADMIN_FEISHU` | 飞书管理员 open_id 列表 | 逗号分隔 |
| `AMBIENT_GROUPS` | 允许潜水接话、学习表情包的群号 | 逗号分隔；留空 = 不在任何群潜水 |

QQ 号、群号、open_id 一律按**字符串**处理。

### `config.py` 分段说明

下表列出常用配置及默认值，完整说明见 `config.py` 内的注释。要修改哪项，在 `config.local.py` 里重新赋值即可。

#### 运行平台与连接

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `PLATFORMS` | `["onebot", "feishu"]` | 同时启用的 adapter |
| `NAPCAT_WS_URL` | `ws://127.0.0.1:3001` | NapCat 正向 WS 地址 |
| `FEISHU_DOMAIN` | `"feishu"` | `"feishu"` = 国内，`"lark"` = 国际版 |
| `FEISHU_LOG_LEVEL` | `"INFO"` | 飞书 SDK 日志级别 |

#### 模型

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `LLM_PROVIDER` | `"openai_compat"` | provider，见 `core/llm.py` 的 `_PROVIDERS` |
| `LLM_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容接口地址 |
| `LLM_MODEL` | `deepseek-flash` | 主对话、接话判断、图片理解统一使用 |
| `LLM_TEMPERATURE` | `0.7` | |
| `LLM_THINKING` | `True` | 私聊和群聊正式回复开启思考 |
| `LLM_REASONING_EFFORT` | `"high"` | 私聊思考强度：`low` / `high` / `max` |
| `GROUP_REASONING_EFFORT` | `"low"` | 群聊思考强度（群聊对延迟更敏感） |
| `LLM_THINKING_MAX_TOKENS` | `16384` | 思考 + 正文的总输出预算 |
| `LLM_MAX_TOKENS` | `2048` | 关闭思考时的输出预算 |
| `LLM_TIMEOUT` | `120.0` | 单次请求超时（秒） |
| `LLM_ENABLE_TOOLS` | `True` | 工具调用总开关（也控制联网搜索） |
| `LLM_MAX_TOOL_ROUNDS` | `20` | 单次对话最多连续调用工具的轮数；`0` = 不限（仍有硬上限 50） |

#### 人设与回复风格

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `SYSTEM_PROMPT` | 猫娘人设 | 所有对话共用的人设；主人昵称和 QQ 号由 `.env` 的 `MASTER_NAME`、`MASTER_QQ` 自动填入 |
| `PRIVATE_PROMPT` | `现在是私聊一对一对话。` | 私聊附加提示 |
| `BOT_NAMES` | `["猫娘", "小猫", …]` | 群友对 bot 的称呼 |
| `REPLY_STYLE_PROMPT` | 默认短答 | 私聊回复长度与分条规则 |
| `REPLY_SEPARATOR` | `<\|new_message\|>` | 模型输出的分条协议，不会发给用户 |
| `REPLY_MESSAGE_DELAY` | `0.8` | 多个气泡之间的发送间隔（秒） |

#### 会话与触发

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `GROUP_TRIGGER` | `["bot", "/chat"]` | 群聊触发前缀（不区分大小写） |
| `MAX_HISTORY` | `20` | 私聊保留的用户消息轮数（含完整工具往返） |
| `HISTORY_TTL` | `3600` | 会话多久没活动就过期（秒） |
| `REPLY_DEBOUNCE` / `REPLY_MAX_WAIT` | `1.0` / `5.0` | 直接互动的合批静默窗口 / 最长等待 |
| `GROUP_IDLE_DEBOUNCE` / `GROUP_IDLE_MAX_WAIT` | `3.0` / `10.0` | 潜水、跟进批次的静默窗口 / 最长等待 |
| `GROUP_HISTORY_MESSAGES` | `100` | 群聊保留的已处理消息数 |
| `GROUP_CONTEXT_CHARS` | `16000` | 群聊时间线的文字预算（字符） |
| `GROUP_REPLY_MAX_MESSAGES` | `6` | 群聊一次最多发几条 |
| `GROUP_AMBIENT_COOLDOWN` | `8.0` | 主动插话冷却（秒） |
| `GROUP_CANCEL_PHRASES` | `["不用回了", …]` | 让 bot 停止当前回复的说法 |
| `RESTART_NOTICE_RETRY_SECONDS` | `5.0` | `/restart` 后通知主人的重试间隔 |

#### 群聊潜水与跟进

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `AMBIENT_CHAT` | `True` | 潜水接话总开关（群名单在 `.env` 的 `AMBIENT_GROUPS`） |
| `AMBIENT_PROBABILITY` | `1.0` | 每批潜水消息调用模型判断的概率 |
| `AMBIENT_REPLY_MODEL` | 同 `LLM_MODEL` | 接话判断使用的模型 |
| `GROUP_GATE_TIMEOUT` | `8.0` | 接话判断超时后保持安静（秒） |
| `GROUP_GATE_PROMPT` | — | 接话判断提示词 |
| `GROUP_DEDUP_GATE` | `True` | 生成期间重复提问的查重开关 |
| `GROUP_DEDUP_PROMPT` | — | 查重提示词 |
| `FOLLOWUP_CHAT` | `True` | bot 回话后一段时间内不用 @ 也能续聊 |
| `FOLLOWUP_WINDOW` | `150` | 跟进窗口（秒） |
| `GROUP_DECISION_PROMPT` | — | 群聊正式回复的提示词与 JSON 输出协议 |

#### 图片

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `IMAGE_MAX_PER_REQUEST` | `8` | 每次请求附带的最近图片数 |
| `IMAGE_MAX_BYTES` | 8 MiB | 单张图片下载上限 |
| `IMAGE_REQUEST_MAX_BYTES` | 16 MiB | 每次请求附图的原始字节总量上限 |
| `IMAGE_CONTEXT_MAX` | `200` | 可按编号回看的图片数 |
| `IMAGE_CACHE_MAX_BYTES` | 64 MiB | `data/images/` 磁盘缓存上限 |
| `IMAGE_TIMEOUT` | `20.0` | 下载超时（秒） |
| `IMAGE_DETAIL` | `"auto"` | `auto` / `low` / `high` / `original` |

#### 持久化、记忆与后台活动

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `DATA_DIR` | `"data"` | 本地数据目录 |
| `SESSION_PERSIST` | `True` | 聊天历史落盘 |
| `SESSION_SAVE_INTERVAL` | `30` | 定时落盘间隔（秒） |
| `MEMORY_MAX` | `200` | 长期记忆上限（超出丢最旧） |
| `AGENT_CONTEXT_ENABLED` | `True` | 共享经历与当前状态 |
| `AGENT_SHARE_EXPERIENCES` | `True` | 所有会话共享经历；`False` = 只对来源会话可见 |
| `AGENT_EXPERIENCES_MAX` | `500` | 近期经历总条数 |
| `AGENT_CONTEXT_CHARS` / `AGENT_CONTEXT_LINES` | `2400` / `20` | 每次注入近况的字符 / 条数上限 |
| `AGENT_LOOP_ENABLED` | `True` | 外层事件循环（改动需完整重启） |
| `AGENT_ACTIVITIES` | `["timer"]` | 自动启动的后台活动 |
| `AGENT_LLM_CALLS_PER_HOUR` | `60` | 后台活动每小时模型调用上限（聊天不计入） |
| `AGENT_WORK_MAX_STEPS` / `AGENT_WORK_TIMEOUT` | `100` / `300.0` | 单项后台工作的步数 / 总时限 |

#### 合并转发、表情包、掉线监控

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `FORWARD_REQUEST_TIMEOUT` | `30.0` | 读取合并转发的超时（秒） |
| `FORWARD_CONTEXT_MAX` | `128` | 全局最多保留多少个转发入口 |
| `FORWARD_PAGE_SIZE` / `FORWARD_PAGE_CHARS` | `20` / `12000` | 每页条数 / 字符预算 |
| `FORWARD_MESSAGE_CHARS` | `3000` | 单条长文本展示上限 |
| `STICKER_LEARN` | `True` | 表情包自动学习总开关 |
| `STICKER_BACKFILL_LEGACY` | `True` | 启动时为旧库补注释 |
| `STICKER_QUEUE_MAX` | `64` | 学习队列上限 |
| `STICKER_REJECT_TTL` | 7 天 | 不收藏的图片多久内不重复判断 |
| `STICKER_RETRY_SECONDS` | `600` | 失败后最短重试间隔 |
| `STICKER_LIST_MAX` | `40` | 提示词中最多列出的表情数 |
| `NAPCAT_WATCH` | `True` | QQ 掉线监控总开关 |
| `NAPCAT_WATCH_INTERVAL` / `NAPCAT_WATCH_FAILS` | `30` / `2` | 探测间隔（秒）/ 连续失败几次判定掉线 |
| `NAPCAT_WATCH_AUTO_RESTART` | `True` | 掉线后自动重启 NapCat |
| `NAPCAT_RESTART_CMD` | `napcat restart <BOT_QQ>` | 重启命令；`BOT_QQ` 为空时为空（不重启） |
| `NAPCAT_RESTART_WAIT` | `15` | 重启后等几秒再读日志 |
| `NAPCAT_LOG_PATH` | `/root/Napcat/log/napcat_<BOT_QQ>.log` | 判断重登结果的日志 |
| `NAPCAT_QR_PATH` | NapCat 缓存目录下的 `qrcode.png` | 登录二维码图片 |

---

## 日常使用

### 什么时候会回复

**私聊**：每条消息都会回复（连发的几条会合成一批再回）。

**群聊**：

| 情况 | 行为 |
| --- | --- |
| @bot（只发一个 @ 也算）、引用 bot 的消息、戳 bot、以 `GROUP_TRIGGER` 开头 | **直接互动**：合批后直接生成正式回复 |
| bot 刚回过话，`FOLLOWUP_WINDOW`（150 秒）内的群消息 | **跟进**：先快速判断是不是在对 bot 说话，是才回复 |
| 位于 `AMBIENT_GROUPS` 的群里的普通消息 | **潜水**：按 `AMBIENT_PROBABILITY` 抽样做快速判断，值得接才插话 |
| 其他 | 只记入群聊历史作为背景，之后 @bot 时模型能看到 |

模型认为不需要回复时会保持安静（例如别人之间在聊、问题已经有人回答）。
群友说「不用回了」「别说了」等（`GROUP_CANCEL_PHRASES`）可以让 bot 停止当前回复。

### 命令速查

命令写不写 `/` 都能匹配（`clear` 和 `/clear` 等价）。群里使用命令需要 @bot 或带触发前缀，
`/content` 和 `/ignore` 例外。

**所有人可用**

| 命令 | 作用 |
| --- | --- |
| `/clear`、`/重置`、`/清空对话` | 清空当前会话的历史和由它产生的经历 |
| `/ignore <内容>` | 这条消息对 bot 完全不可见（不进上下文、不触发回复、不学表情），任何地方都不用 @ |
| `/state` | 查看当前会话可见的近况与状态 |
| `/agent`、`/agent status` | 查看外层循环、后台活动和进行中的请求 |

**仅主人（大部分要求私聊）**

| 命令 | 作用 |
| --- | --- |
| `/content <消息>` | 预览「如果发这条消息」时模型会收到的完整请求，结果私发给主人；群里直接发即可 |
| `/state set <备注>` / `/state share <备注>` / `/state clear` | 设置仅本私聊可见 / 所有会话可见的状态备注，或清除 |
| `/agent start <活动>` / `/agent stop <活动>` | 启停后台活动（如 `timer`） |
| `/agent demo [步数] [间隔秒]`、`/agent pause` / `resume` / `stop` | 离线演示分步工作，以及暂停、继续、停止 |
| `/reload` | 热重载 `plugins/` 和 `tools/`，失败自动回滚 |
| `/update` | `git pull --ff-only` 并热重载；改动涉及核心代码时提示需要 `/restart` |
| `/restart` | 原地重启整个进程；QQ 恢复后私聊通知主人 |
| `/git <参数>` | 在仓库中执行 git（HEAD 变化时自动热重载） |
| `/rollback [n]` | `git reset --hard HEAD~n`（默认 1）并热重载 |
| `/backup`、`/export` | 在线导出完整状态到本机 ZIP |
| `/probe`、`/ping` | QQ 链路诊断：adapter 能力、NapCat 版本、各动作结果 |
| `/login` | 把当前 NapCat 登录二维码发给主人 |
| `/napcat` | 重启 NapCat；快速登录失败时把二维码发给主人 |
| `/meaning <id> <含义>` | 手动修正表情包含义 |
| `/pending` | 查看旧表情库补注释进度 |

非主人调用主人命令会收到提示，并附上对方的 id，方便填配置。

### 模型可以调用的工具

工具由模型按需调用，无需命令。`LLM_ENABLE_TOOLS = False` 可全部关闭。

| 工具 | 作用 |
| --- | --- |
| `web_search` | DeepSeek 官方网页搜索，每次最多 5 条结果，回答附来源链接 |
| `remember` / `list_memories` / `forget` | 全局长期记忆 |
| `remember_contact` / `list_contacts` / `forget_contact` | 通讯录：记住人和群的 id，之后按名字找 |
| `send_message` | 给任意人或群发消息，可指定从 QQ 或飞书发（不能发给主人） |
| `contact_master` | 私聊主人（汇报、求助、转达） |
| `send_poke` | 戳一戳群成员或好友 |
| `look_image` | 按编号回看较早的图片 |
| `read_forward` | 逐层、分页读取 QQ 合并转发 |
| `send_sticker` / `list_stickers` / `forget_sticker` | 发送、列出、删除收藏的表情包 |
| `request_action` | 请后台活动做事（如定时提醒） |
| `get_current_time` / `roll_dice` 等 | `tools/examples.py` 中的示例工具 |

---

## 功能详解

### 群聊：合批、接话判断与多条回复

**合批**（`core/coalesce.py`）：每个会话只有一个消费者。模型生成和发送期间来的新消息只累积为下一批，
不会排起一串模型请求。

- **有直接互动**时，从第一条直接互动开始计时：被回应的人静默 `REPLY_DEBOUNCE`（1 秒）后开始处理，
  最多等 `REPLY_MAX_WAIT`（5 秒）。只有提问者自己继续补充才延长等待，别人刷屏不会推迟回答。
- **只有潜水 / 跟进消息**时不赶时间：全群静默 `GROUP_IDLE_DEBOUNCE`（3 秒）或最多 `GROUP_IDLE_MAX_WAIT`
  （10 秒）后才判断，同一话题攒成一批，避免每条都接一次话。等待期间来了直接互动会立即切换节奏。
- 积压超过单批预算时，最早的直接提问优先，排在它前面的闲聊作为背景。

**接话判断**：没有直接互动的批次，先用同一模型、关闭思考和工具，只读文字时间线，输出 `REPLY` 或 `SKIP`。
判断失败或超过 `GROUP_GATE_TIMEOUT` 时保持安静。同群有人 @ 时会打断正在等待的判断。

**生成期间的重复提问**：bot 生成回复时，群友可能继续追问。这些消息发出时对方还没看到 bot 的回复，
bot 生成时也没看到它们。时间线会给它们标上 `"先于BOT回复": true`，提示模型不要把同一件事再答一遍。
如果一批里的直接互动**全部**带这个标记，正式回复前会先做一次快速查重（`GROUP_DEDUP_PROMPT`）：
只有明确输出 `SKIP` 才跳过（催促、重复、附和、道谢）；有新问题、补充、纠正，或者查重失败 / 超时，都照常回复。
`GROUP_DEDUP_GATE = False` 可关闭。

**正式回复协议**：群聊回复要求模型只输出 JSON，可以一次发多条、每条独立选择引用：

```json
{"messages": [
  {"text": "回答甲的问题", "reply_to": "甲的消息ID"},
  {"text": "自然接续，不引用"},
  {"text": "回答乙的问题", "reply_to": "乙的消息ID"}
]}
```

- `messages: []` 表示保持安静；最多 `GROUP_REPLY_MAX_MESSAGES` 条。
- `reply_to` 只能用上下文中真实存在的消息 ID，省略或 `null` 表示不引用。
- 先校验整个 JSON 再按顺序发送；协议不合法或生成失败时，不会把 JSON 或错误发进群。
- 工具调用阶段的中间正文不发送；已执行的工具结果即使最终生成失败也会留下记录。

**取消**：新来的停止要求（`GROUP_CANCEL_PHRASES`，来自同批参与者或直接对 bot 说）会阻止后续正文发送和
尚未执行的工具。已经完成的发送或工具操作不会撤销，普通新消息也不会取消当前回答。

**冷却**：`GROUP_AMBIENT_COOLDOWN` 内只回应直接互动或真正的跟进；未开启跟进时，冷却期内的潜水消息只记录不判断。

### 私聊与消息分条

私聊保留最近 `MAX_HISTORY` 轮用户消息，包括每轮完整的 assistant / tool 往返。
AI 默认只回一句短话；需要说几句时，模型用 `REPLY_SEPARATOR` 分条，发送层拆成多个消息气泡，
相邻气泡间隔 `REPLY_MESSAGE_DELAY` 秒以降低乱序概率。代码块、清单等需要整体阅读的内容不拆。

### 对话历史与群聊时间线

- 历史保存在 `data/sessions.json`，每 `SESSION_SAVE_INTERVAL` 秒及退出时保存；只存图片引用，不存原图。
- 群聊保存消息 ID、发送者、时间、@ 对象、引用对象和正文。模型看到的时间线区分「此前历史」和「本轮新增」，
  优先补入被引用的原文和最近的 bot 发言；看不到的引用原文不猜测。
- `GROUP_CONTEXT_CHARS` 控制时间线文字预算（按字符，不是精确 token）；过长的单条正文只在请求副本中截断，
  存储原文保持完整。超出预算的待处理消息分批消费，不会被刷屏挤掉。
- 重启后恢复的历史只作为背景，不会补发关机前没处理完的批次。
- 会话按「平台 + 会话」隔离（如 `onebot_group_123456`、`feishu_private_ou_xxx`）。

### 图片与多模态

主对话、群聊决策、图片回看统一使用 `deepseek-flash` 的原生图文输入，不需要单独的视觉模型。

- QQ 图片 / 表情、飞书图片和富文本中的图片直接进入对话；纯图片、多张图、连续发图都能直接提问。
- 入站时只登记图片引用，合批结束、正式回复时才下载附图。接话判断只读文字和图片标记。
- 每次请求优先附最近 `IMAGE_MAX_PER_REQUEST` 张；更早的图片保留 `[图片#xxxx]` 编号，模型可调用 `look_image` 回看。
- 下载失败、过大或格式不支持时保留文字，并明确标出图片不可用。
- 原图缓存在 `data/images/`，按 `HISTORY_TTL`、张数和字节数清理；缓存有效时重启后还能追问。

### 合并转发与戳一戳（QQ）

**合并转发**：模型只看到 `[合并转发#xxxx]` 入口，即使协议端附带了完整内容也不会自动注入。
需要时模型调用 `read_forward(forward_id, offset, limit)`：

- 每次只展开一层，保留发言人、时间、文本、图片入口和子转发入口；嵌套转发由模型决定是否继续打开。
- 分页：单页最多 `FORWARD_PAGE_SIZE` 条、`FORWARD_PAGE_CHARS` 字符，`next_offset` 非空表示还有下一页；
  超过 `FORWARD_MESSAGE_CHARS` 的长文本会标记截断。
- 入口绑定当前会话，不能跨群 / 私聊使用；受 `FORWARD_CONTEXT_MAX` 与 `HISTORY_TTL` 限制，重启或过期后需重新发送。
- 转发内容始终只是引用材料，不能充当当前用户的指令或身份凭据。

**戳一戳**：群聊 / 私聊的戳一戳会进入上下文，保留操作者和目标；群里戳 bot 算直接互动。
模型可以调用 `send_poke(user_id?)` 主动戳人或戳回，不会固定自动回戳；等 NapCat 确认成功后才告诉模型已发送，
超时不自动重试。失败时返回动作名、`retcode` 和原因分类，不把堆栈或凭据放进模型上下文。

### 联网搜索

私聊直接说「上网查一下 DeepSeek 的搜索接口怎么收费，给我官方链接」，群里 @bot 问同样的问题即可。
模型提取关键词调用 `web_search`，根据标题、日期和摘要作答并附来源链接。

- 搜索固定调用 DeepSeek 官方接口，Key 规则见 [`.env` 键一览](#env-键一览)。
- 受 `LLM_ENABLE_TOOLS` 总开关控制。
- 官方按有结果的成功搜索收费（每千次 5 元），失败或无结果免费。参见
  [DeepSeek 网页搜索文档](https://api-docs.deepseek.com/zh-cn/guides/search/)。

### 长期记忆与通讯录

**长期记忆**：bot 有一本全局笔记（`data/memories.json`），通过 `remember` / `list_memories` / `forget` 自己管理。
记下的内容会自动注入之后每次回复的系统提示，所以它「始终记得」。全局共享，跨群跨平台可见，上限 `MEMORY_MAX` 条。

> 例：群里说「记住我生日是 6 月」→ bot 调用 `remember` → 重启后再问仍然记得。

**通讯录**：`remember_contact` / `list_contacts` / `forget_contact` 把人和群的 id 记到 `data/contacts.json`。
之后群友说「帮我提醒张三带水」，bot 可以查到张三的 id，再用 `send_message` 私聊他，也可以跨平台发送。

### 表情包自动收藏与发送

bot 用同一个多模态模型看原图，自己判断**是不是表情包、值不值得收藏**。两项都通过、生成了完整注释、
原图落盘成功，才写入全局表情库（`data/stickers.json` + `data/stickers/`）。学习过程不发消息、不问人。

- **收集范围**：只收集 `AMBIENT_GROUPS` 群里别人发的图片，不收集 bot 自己发的。普通照片、截图默认不收，拿不准也跳过。
  判断标准在 `STICKER_ANALYZE_PROMPT`，不保证每张都判断正确。
- **注释**：收藏时生成 `meaning`（简短含义）、`comment`（画面、情绪、适用场景）、`tags`（检索标签）。
  `send_sticker` 可按含义、标签、场景或 id 查找发送；每次回复最多发一个表情。
- **后台执行**：有界队列串行处理，不阻塞聊天；同一图片不并发重复识别，原图按内容去重。
- **避免重复请求**：不收藏的判断记录保留 `STICKER_REJECT_TTL`（7 天），失败后至少等 `STICKER_RETRY_SECONDS` 再试；
  记录在 `data/sticker_decisions.json`。
- **旧库兼容**：已有的人工含义不会被覆盖；启动时后台为没有含义的旧图补注释，判定不适合的旧图只标记不删除。
  主人可用 `/meaning <id> <含义>` 修正，`/pending` 查看补注释进度，模型可用 `forget_sticker` 删除不合适的条目。

`STICKER_LEARN = False` 关闭学习和补注释；`STICKER_BACKFILL_LEGACY = False` 只关闭补注释。

### 共享经历与当前状态

`core/timeline.py` 在一份近期记录里保存收到的聊天、实际发送的尝试与结果、工具调用状态，保存在 `data/agent_state.json`。

- 默认**所有会话共享经历**（`AGENT_SHARE_EXPERIENCES = True`）：私聊里说的事群里知道，群里发生的事私聊问得到。
  来自其他会话的条目带 `from`（如 `QQ群123456`、`后台·定时提醒`）和 `who`；别处的工具调用过程不注入。
  设为 `False` 时，经历只对来源会话可见。
- 每次最多注入 `AGENT_CONTEXT_LINES` 条、`AGENT_CONTEXT_CHARS` 字符；预算不够时先保留后台活动结果，
  再按新到旧保留对话，工具调用最先舍弃。超过 `HISTORY_TTL` 的经历不再注入。
- 「尝试发送」「平台确认」「结果未确认」「工具返回」分开记录：模型生成的文字不等于已经发出，工具返回不等于业务成功。
- `/clear` 会清掉当前会话产生的经历；已发给其他会话的内容仍算对方收到的记录。
- **状态备注**：主人私聊 `/state set <备注>`（仅该私聊可见）或 `/state share <备注>`（所有会话可见），
  最多 480 字，只保留一份。备注只是信息，不会启动任何任务。
- 数据文件损坏或版本过新时会保留原文件、停用本功能，正常聊天不受影响。

### 后台活动与外层循环

`core/agent.py` 是外层事件循环：用一个队列接收消息、工作进度、结果和控制请求。消息仍走原来的
「分发 → 合批 → 私聊 / 群聊处理」；后台工作与消息分发分开运行，耗时任务不会堵住收件箱。

**后台活动**是聊天之外持续进行的事。聊天里的模型不能直接操作活动，只能通过 `request_action` 提交请求。
内置活动「定时提醒」（`timer`）的完整流程：

1. 有人说「十分钟后提醒我喝水」，模型调用
   `request_action(activity="timer", action="remind", args={"seconds": 600, "text": "喝水"})`。
2. 外层循环检查活动在运行、动作存在、请求人有权限，再交给活动**当场**决定：接受（之后完成并通知）、
   已完成，或拒绝并说明原因。模型按真实结果回话，接受时不会说已经做完。
3. 到点后，结果记入经历，并作为一条**后台通知**送回发起的会话：群聊里发言人是固定的 `agent-notice`
   （真人账号不可能是这个 ID，自称后台通知的普通消息不可信），私聊里在正文标明来源。
   然后由原有群聊 / 私聊流程决定怎么说，群聊可以引用原请求消息。

细节：

- 定时提醒：10 秒到 24 小时，最多同时 20 个，所有人可请求；只有发起人或主人能取消。
- 动作分「所有人可请求」和「仅主人可请求」，可用动作会写进 `request_action` 的工具说明。
- 请求和提醒只在内存中，进程退出时取消，重启后需要重新设置。
- 后台活动每小时调用模型的次数受 `AGENT_LLM_CALLS_PER_HOUR` 限制，用完后暂停思考并私聊提醒主人一次（定时提醒不调用模型）。
- 同一时间最多运行一项分步后台工作（`/agent demo` 可演示），步数和总时长受 `AGENT_WORK_MAX_STEPS` / `AGENT_WORK_TIMEOUT` 限制；
  重启前未完成的工作标记为「进程中断」，不自动续跑。
- `AGENT_LOOP_ENABLED = False` 并完整重启后，消息直接走旧的聊天入口。

### 预览模型上下文：`/content`

主人在 QQ 或飞书发 `/content 刚才聊到哪了？`，会在该平台的主人私聊收到预览：
把参数当作普通消息时的首轮请求 JSON，包括系统提示、该会话可见的记忆和近况、裁剪后的历史、工具定义和模型参数。

- 群里直接发即可，无需 @bot；预览使用当前群的历史，保留这条消息的 @ 和引用关系，并标明这批消息会触发
  正式回复、接话判断、查重还是只作背景。
- 预览不调用模型、不执行工具或命令，不写历史、不触发表情学习、不改变待处理批次。长预览分段私发。

### 忽略单条消息：`/ignore`

在消息开头加 `/ignore`，整条消息（含图片）在中间件和命令分发之前就被丢弃：不进上下文、不触发回复和工具、
不学表情、不影响正在等待的批次。群里无需 @，任何人可用，不会收到确认回复。只有开头的 `/ignore` 生效，
正文中间出现不会被误拦截。

### 思考与延迟

群聊回复通常很短，延迟主要花在思考上，所以 `GROUP_REASONING_EFFORT` 默认 `low`，私聊用 `high`。
接话判断、查重和表情学习始终关闭思考。思考截断或空正文会返回明确提示，不保存空回答、不执行被截断的工具调用。

群聊系统提示把基本不变的内容（人设、长期记忆、表情索引、通讯录、输出协议）放在前面，每次都变的近况和
本批策略放在最后，让连续请求的前缀相同，更容易命中 DeepSeek 的
[上下文硬盘缓存](https://api-docs.deepseek.com/zh-cn/guides/kv_cache)。

每次请求会在日志里记一行：

```
[LLM] 6.2s 思考=low 输入=5321(缓存命中4864) 输出=212(推理180)
```

推理 token 多说明思考太长，缓存命中低说明提示前缀变了，耗时长但 token 少说明 API 本身慢。

接口依据：[DeepSeek 图像指南](https://api-docs.deepseek.com/zh-cn/guides/vision/)、
[思考模式说明](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)。

---

## 运维

### 用 systemd 常驻运行（示例）

```ini
# /etc/systemd/system/nekobot.service
[Unit]
Description=nekobot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/nekobot
ExecStart=/root/.local/bin/uv run --locked bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now nekobot
```

- 路径按实际安装位置修改（`which uv` 查看 uv 路径）。
- bot 收到 SIGTERM 会先保存会话再退出，`systemctl stop` 是安全的。
- `/restart` 在同一进程内原地重启，systemd 不会把它当成崩溃。
- 密钥也可以不放 `.env`，改用 `Environment=` / `EnvironmentFile=` 注入，进程环境变量优先于 `.env`。

### 远程运维（主人私聊）

`plugins/admin.py` 的命令在 QQ 和飞书私聊都能用；QQ 掉线时从飞书发尤其方便。

- `/reload`：热重载 `plugins/` 和 `tools/`，不重启进程。`core/hotreload.py` 原地清空注册表并重新导入，
  **失败自动回滚**，坏改动不会搞挂正在运行的 bot。
- `/update`：`git pull --ff-only` 并热重载。改动涉及 `core/`、`config.py`、`bot.py` 或依赖文件时会提示需要 `/restart`。
- `/restart`：替换进程映像原地重启，各平台自动重连。QQ 恢复后私聊 `MASTER_QQ` 通知「重启完成」（无论命令从哪个平台发起），
  发送失败每 `RESTART_NOTICE_RETRY_SECONDS` 秒重试。备份导出进行中时会拒绝重启。
- `/git <参数>`：直接在仓库里执行 git（不经 shell），HEAD 变化时自动热重载。
- `/rollback [n]`：回退 n 个提交并热重载。
- `/napcat`：执行 `NAPCAT_RESTART_CMD`，等几秒读日志，快速登录失败就把登录二维码发给主人。

### 更新与升级

| 改了什么 | 需要做什么 |
| --- | --- |
| `plugins/`、`tools/` | `/reload`（`/update` 会自动做） |
| `core/`、`adapters/`、`config.py`、`bot.py` | `/restart` |
| `.env`、`config.local.py` | `/restart` |
| 依赖（`pyproject.toml` / `uv.lock`） | 先在服务器运行 `uv sync --locked`，再 `/restart` |

标准更新流程：主人私聊 `/update`；如果提示需要重启，再发 `/restart`。

> **从旧版本升级**：早期版本把密钥直接写在 `config.py` 里。升级到 `.env` 版本前，先在服务器上按
> `.env.example` 创建 `.env` 并填好密钥和账号，再更新代码、重启；否则重启后 QQ、飞书和模型都会因缺少凭证而无法连接。

> **改过 `config.py` 的部署**：个人配置现在放在 `config.local.py`。服务器上的 `config.py` 有本地改动时，
> `/update` 会因冲突失败。在服务器上执行一次迁移：
>
> ```bash
> git stash push -- config.py      # 暂存你对 config.py 的改动
> git pull --ff-only               # 更新代码
> python3 configure.py migrate     # 把暂存里改过的配置项写进 config.local.py（会先列出来让你确认）
> git stash drop                   # 确认无误后丢弃暂存
> ```
>
> 之后发 `/restart`。迁移只处理 `NAME = 值` 形式的配置项，注释等其他改动不会带过去。

### QQ 掉线监控

NapCat 上的 QQ 常被风控踢下线，而此时 OneBot 连接不报错，NapCat 自己也报告在线
（`get_status` / `get_login_info` 没用，自发私聊也不回环）。实测唯一可靠的信号是
`get_stranger_info(BOT_QQ, no_cache=True)`：强制向服务器查询，在线时正常返回，掉线时超时无响应。

`core/napcat_watch.py` 每 `NAPCAT_WATCH_INTERVAL` 秒探测一次，连续 `NAPCAT_WATCH_FAILS` 次无响应判定掉线：

1. 飞书私聊主人告警；
2. （`NAPCAT_WATCH_AUTO_RESTART`）执行 `NAPCAT_RESTART_CMD` 重启 NapCat；
3. 等 `NAPCAT_RESTART_WAIT` 秒，读 `NAPCAT_LOG_PATH` 的新增日志：出现 `NAPCAT_RELOGIN_ERROR`（快速登录失败）时，
   把 `NAPCAT_QR_PATH` 的二维码推到飞书，主人扫码即可恢复；没有报错则多半已快速登录成功；
4. 恢复在线后再报一条。

前提：`PLATFORMS` 同时包含 `"onebot"` 和 `"feishu"`，并配好 `BOT_QQ` 和 `MASTER_FEISHU`。
手动排查用 `/probe`（链路诊断）和 `/login`（获取二维码）。

### 日志

日志输出到标准输出（前台运行看终端，systemd 下用 `journalctl -u nekobot -f`）。常用关键字：

| 关键字 | 含义 |
| --- | --- |
| `启动平台适配器` / `适配器退出` | adapter 启停；单个平台出错不影响其它平台 |
| `[私聊] (id)` | 收到私聊，可从这里拿到自己的 QQ 号 / 飞书 open_id |
| `[LLM]` | 每次模型请求的耗时与 token 用量 |
| `[合并转发] 已接收` / `已写入上下文` | 合并转发的接收与入历史 |
| `[掉线监控]` | QQ 掉线检测与恢复过程 |
| `[重启通知]` | `/restart` 后通知主人的过程 |
| `[运维]` / `[备份]` | 远程运维命令与备份导出 |

---

## 数据与备份

### 数据目录

运行时数据都在 `DATA_DIR`（默认 `data/`，已在 `.gitignore`）：

| 路径 | 内容 |
| --- | --- |
| `sessions.json` | 私聊 / 群聊历史（只存图片引用） |
| `agent_state.json` | 共享经历、状态备注、后台工作摘要 |
| `memories.json` | 长期记忆 |
| `contacts.json` | 通讯录 |
| `stickers.json` + `stickers/` | 表情库索引与原图 |
| `sticker_decisions.json` | 表情学习的判断记录 |
| `images/` | 图片缓存 |

JSON 文件采用原子写入（先写临时文件再替换），异常中断不会写出半个文件。

### 全量导出与恢复

`backup.py` 只依赖 Python 标准库和 Git，不需要先安装 bot 依赖，适用于 Linux / macOS 的 Git 工作副本。

**归档内容**

- 实际配置：`config.py`、`config.local.py`、`.env` / `.env.*`、Compose 配置、`config/` / `configs/` 目录；
  `DATA_DIR` / `STICKER_DIR` 写在 `config.local.py` 里时以它为准，恢复时需要改写的路径也写进 `config.local.py`；
- 完整的 `DATA_DIR`（包括将来新增的数据文件，以及部署在其中的 NapCat 配置、QQ 登录数据等）；
- 仓库中其他未被 Git 跟踪的本地文件，例如自建插件。排除 `.git`、虚拟环境、编译缓存、`backups/` 等。

仓库源码和锁文件不打包。**备份包含密钥、私聊和登录数据，没有加密**，文件权限为 `0600`，请妥善保管，
只恢复自己信任的备份。工具不会导入或执行归档中的 Python 配置。

**在线导出（推荐）**：主人私聊 `/backup`（或 `/export`）。bot 捕获当前内存中的聊天、记忆、通讯录和表情状态，
在后台生成 `backups/nekobot-backup-*.zip`，完成后回复本机路径，不需要停机。

- SQLite 数据库通过在线备份接口导出一致的已提交事务；其他文件复制时检查变动并有限重试，不会生成半成品 ZIP。
- 即使 `SESSION_PERSIST = False`，内存中的会话也会写进备份。
- 在线导出不保证多个外部服务之间处于同一事务时刻；需要严格一致时请停服后离线导出。

**离线导出与恢复**：先正常停止 bot，以及 NapCat 等会写入这些目录的服务（Compose 部署可用 `docker compose stop napcat`）。
运行中的 bot 与命令行导出 / 恢复互斥（共用维护锁），但维护锁不会替你停止外部服务。

```bash
# 预览将被包含的文件（不打印密钥和聊天正文）
python backup.py export --dry-run
```

```bash
# 导出到 backups/nekobot-backup-时间-随机值.zip；也可在后面指定输出路径（已有文件不会被覆盖）
python backup.py export
```

```bash
# 校验格式、文件大小和每个文件的 SHA-256；--list 列出文件
python backup.py inspect /安全目录/neko-state.zip --list
```

```bash
# 在已停服的目标仓库中预览恢复
python backup.py restore /安全目录/neko-state.zip --dry-run
```

```bash
python backup.py restore /安全目录/neko-state.zip
```

**迁移到新机器**：克隆兼容版本的仓库，把 ZIP 放在 `backups/` 或仓库外，执行恢复（可加 `--root /目标/仓库`）。

- 恢复先在临时目录验证全部文件再替换；数据子目录整体替换，不混入目标目录里的旧记录，源码不变。
- 原数据移至 `backups/before-restore-*/original/`，`recovery.json` 记录替换过的路径，便于人工回退；
  替换出错会自动撤回本次已完成的替换。若进程被强杀或断电，先按清单检查，不要启动 bot。
- 仓库外的绝对数据目录会收集到归档的 `data/`，恢复时同步改写 `DATA_DIR` / `STICKER_DIR` 及表情索引路径。
- 额外服务的数据可用 `--include /srv/extra/data=services/extra-data` 显式加入（可重复）。
- 不恢复系统服务、容器镜像、ACL / xattr；恢复后检查挂载路径和权限再启动服务。异机 QQ 登录可能仍需扫码。

---

## 开发与扩展

### 目录结构

```
bot.py               # 入口：获取进程锁、创建 adapter、事件分发、定时落盘
config.py            # 行为配置的默认值（密钥从 .env 读取）
config.local.py      # 个人配置，覆盖 config.py 的默认值（gitignore，自行创建）
.env.example         # .env 模板
backup.py            # 离线导出 / 校验 / 恢复命令行
configure.py         # 交互式配置 .env / config.local.py 的命令行工具
adapters/            # 平台适配器（自动注册）
  onebot.py          #   OneBot/QQ：WS 长连接、消息段解析与发送
  onebot_messages.py #   QQ 消息段 / CQ 码、转发入口与单层内容解析
  onebot_errors.py   #   OneBot 错误码与原因分类
  feishu.py          #   飞书/Lark：lark-oapi 长连接、消息归一与发送
core/                # 平台无关的框架核心
  env.py             #   读取 .env 与环境变量
  local_config.py    #   加载 config.local.py，重新计算依赖它的默认值
  bot.py             #   Bot 发送抽象 + 富消息段（TextSeg / ImageSeg）
  adapter.py         #   Adapter 抽象 + 平台注册表
  event.py           #   平台无关的 MessageEvent（to_me / command_text / session_key …）
  plugin.py          #   插件注册表与分发（含 /ignore 拦截）
  middleware.py      #   中间件链
  tools.py           #   LLM 工具注册表
  llm.py             #   LLM provider 抽象 + OpenAI 兼容实现
  prompts.py         #   提示词拼装（人设、记忆、表情、通讯录）
  coalesce.py        #   每会话单消费者的消息合批
  group_chat.py      #   群聊接话判断、查重、正式回复与发送
  group_context.py   #   带引用与新增边界的群聊时间线
  private_chat.py    #   私聊入口
  replies.py         #   回复分条与发送
  session.py         #   会话历史与持久化
  timeline.py        #   共享经历与当前状态
  agent.py           #   外层循环：收件箱、分步工作、后台活动、请求仲裁、模型预算
  activity.py        #   后台活动接口（Action / Request / Verdict / Activity）
  activities/        #   内置后台活动（timer）
  notices.py         #   把活动结果作为后台通知送回发起会话
  images.py          #   图片登记、下载、缓存与 look_image 后端
  forwards.py        #   合并转发入口登记与缓存
  memory.py          #   长期记忆
  contacts.py        #   通讯录
  stickers.py        #   表情库
  sticker_learning.py#   表情包后台识别与注释
  engagement.py      #   跟进窗口状态
  perms.py           #   主人判定与 master_only 装饰器
  runtime.py         #   运行时上下文（当前 event / bot、各平台 adapter）
  napcat_watch.py    #   QQ 掉线监控
  restart_notice.py  #   /restart 后通知主人
  hotreload.py       #   plugins / tools 热重载（失败回滚）
  storage.py         #   JSON 原子读写
  backup.py          #   备份清单、校验、路径修复、恢复回滚
  live_backup.py     #   在线导出：内存快照、SQLite 在线备份
  maintenance.py     #   进程互斥锁
  log.py             #   日志配置
plugins/             # 业务插件（自动发现）
  chat.py            #   默认聊天（私聊 / 群里被触发）
  ambient.py         #   群聊潜水与跟进
  builtin.py         #   /clear
  content.py         #   /content
  state.py           #   /state
  agent.py           #   /agent
  stickers.py        #   表情学习入口、/meaning、/pending
  probe.py           #   /probe、/login
  admin.py           #   /reload /update /restart /git /rollback /backup /napcat
  examples.py        #   示例中间件（默认关闭）
tools/               # LLM 工具（自动发现）
  web_search.py  memory.py  contacts.py  message.py  master.py  poke.py
  vision.py  forward.py  sticker.py  activity.py  examples.py
tests/               # unittest 测试
data/                # 运行时数据（gitignore）
```

### 架构

```
IM 平台(QQ/飞书) ──> Adapter ──归一──> MessageEvent ──> 中间件 ──> 插件分发 ──> 合批 ──> LLM(+工具)
                        │                                                              │
                        └──────────────────── Bot（发送）<─────────────────────────────┘
```

- **Adapter**（`core/adapter.py`）：连接平台，把原始事件归一成 `MessageEvent` 交给上层；`bot` 属性是该平台的发送端。
- **Bot**（`core/bot.py`）：发送能力抽象。消息统一用 `TextSeg` / `ImageSeg` 表示，`str` 视作单个 `TextSeg`；
  所有 id 统一为 `str`。
- **MessageEvent**（`core/event.py`）：adapter 填基础字段，core 计算派生字段（`to_me`、`command_text`、
  `session_key`、`llm_text` 等）。会话键带平台前缀，不同平台互不干扰。
- 核心模块、插件和工具都面向抽象编程，不感知底层协议。启用了外层循环时，消息先进入 `core/agent.py` 的收件箱再分发。

### 1. 写一个插件（命令 / 关键词 / 正则 / 兜底）

在 `plugins/` 下新建文件即可被自动加载：

```python
from core.plugin import on_command

@on_command("hello", priority=5)
async def hello(event, bot, match):
    await bot.reply(event, "你好喵~ " + match.text)
```

- 装饰器：`on_command(*names)`、`on_prefix(*prefixes)`、`on_keyword(*words)`、`on_regex(pattern)`、`on_message()`。
- 通用参数：`priority`（越小越先匹配）、`scope`（`all` / `private` / `group`）、`to_me`（群聊是否需要 @ 或前缀，默认 `True`）、
  `admin_only`（只允许 `ADMIN_QQ` / `ADMIN_FEISHU`）、`block`（命中后是否停止分发，默认 `True`）。
- handler 签名固定为 `async def handler(event, bot, match)`，`match.text` 是去掉命令 / 前缀后的正文。
- 只允许主人使用：叠加 `core.perms.master_only` 装饰器。
- 改完发 `/reload` 即可生效。

### 2. 写一个中间件（黑名单 / 限流 / 日志）

```python
from core.middleware import middleware

@middleware(priority=10)
async def block_user(event, bot) -> bool:
    if event.user_id in BANNED:
        return False   # 返回 False 拦截，终止后续中间件与插件
    return True
```

参考 `plugins/examples.py`。

### 3. 接入新的 LLM 供应商

OpenAI 兼容接口只需在 `config.local.py` 里设置 `LLM_BASE_URL`、`LLM_MODEL`，并在 `.env` 里填 `LLM_API_KEY`。
注意图片输入和思考参数需要该接口支持；非 DeepSeek 官方地址时联网搜索默认关闭，可单独配置 `DEEPSEEK_SEARCH_API_KEY`。

协议不兼容时，在 `core/llm.py` 写一个 `LLMProvider` 子类：

- 实现带 `model`、`thinking`、`max_tokens` 参数的 `chat()`，支持可选的 `response_format`（表情识别使用 JSON 模式）；
- 返回 `ChatResult(text, messages, ok)`：`text` 包含各轮完整的用户可见正文（可用 `REPLY_SEPARATOR` 分隔），
  不要只返回最后一轮，也不要混入推理内容或工具返回值；`messages` 是本次新产生的完整 assistant / tool 消息，不含输入图片；
- 原生图片引用可用 `core.images.prepare_messages()` 转换；
- 注册进 `_PROVIDERS`，再把 `config.LLM_PROVIDER` 改成对应的键。

思考模式下，只要请求带有 `tools`，历史 assistant 消息里的 `reasoning_content` 就需要完整回传。
工具执行后的续请求会带回该轮推理内容；未携带 `tools` 时，DeepSeek 会忽略历史推理内容。

### 4. 给 LLM 加工具（function calling）

在 `tools/` 下新建文件，用 `@tool` 注册函数（同步 / 异步皆可）：

```python
from core.tools import tool

@tool(description="获取指定城市的天气", parameters={
    "type": "object",
    "properties": {"city": {"type": "string", "description": "城市名"}},
    "required": ["city"],
})
async def get_weather(city: str) -> str:
    return f"{city} 晴 25℃"
```

- `parameters` 是标准 JSON Schema，省略表示无参数；`name` 默认取函数名。
- `scope` 默认 `"chat"`，只有 `chat` 场景的工具会出现在聊天请求里；后台活动专用的工具用其他 scope。
- 需要当前消息上下文时用 `core.runtime.get_event()`。
- 参考 `tools/examples.py`，改完 `/reload`。

### 5. 接入新平台（adapter）

在 `adapters/` 下新建 `xxx.py`：

1. 实现 `Bot` 子类：把 `TextSeg` / `ImageSeg` 翻译成平台消息格式发送；
2. 实现 `Adapter` 子类：连接平台，把事件归一成 `MessageEvent` 交给 `on_event`；
3. 末尾 `register_adapter("xxx", XxxAdapter)`，并在 `adapters/__init__.py` 中 import；
4. 把 `"xxx"` 加进 `config.PLATFORMS`。

参考 `adapters/onebot.py`（WebSocket 直连）和 `adapters/feishu.py`（SDK 长连接：阻塞的 `start()` 放到线程里跑，
同步回调投回主事件循环）。

### 6. 写一个后台活动

在 `core/activities/` 下继承 `core.activity.Activity`：

```python
import asyncio
from contextvars import Context

from core.activity import Action, Activity, Request, Verdict


class EchoActivity(Activity):
    name = "echo"
    title = "复读"
    actions = (
        Action("say", "过 5 秒把 text 发回发起的会话", {"text": {"type": "string"}}, everyone=True),
    )

    def __init__(self) -> None:
        super().__init__()
        self._tasks: set[asyncio.Task] = set()

    def accept(self, request: Request) -> Verdict:
        # 同步、快速地当场决定：accepted = 之后完成并通知；done = 已当场完成；rejected = 拒绝并说明原因
        text = request.args.get("text")
        if not isinstance(text, str) or not text.strip():
            return Verdict.rejected("text 不能为空")
        # 实际工作放进自己的任务；新的 Context 让它不继承当前聊天的 event/bot 身份
        task = asyncio.get_running_loop().create_task(self._later(request.id, text), context=Context())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return Verdict.accepted("5 秒后复读")

    async def _later(self, request_id: int, text: str) -> None:
        await asyncio.sleep(5)
        self.ctx.finish(request_id, True, f"复读：{text}")  # 记入经历并作为后台通知送回发起会话

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def status(self) -> str:
        return f"{len(self._tasks)} 条复读在等"  # 一句话现状，注入各会话的自我状态
```

- 在 `core/activities/__init__.py` 的 `BUILTIN` 中登记，加入 `config.AGENT_ACTIVITIES` 即自动启动，也可用 `/agent start echo`。
- `Action(everyone=False)` 表示仅主人可请求。
- 需要调用模型前先检查 `self.ctx.allow_llm()`（受 `AGENT_LLM_CALLS_PER_HOUR` 限制）。
- 完整示例见 `core/activities/timer.py`。改动属于 `core/`，需要 `/restart`。

### 测试

```bash
uv run --locked python -m unittest discover -s tests -v
```

### 依赖管理

依赖定义在 `pyproject.toml`，精确版本锁定在 `uv.lock`。修改依赖后：

```bash
uv lock
```

```bash
uv export --locked --no-dev --no-emit-project --no-hashes --output-file requirements.txt
```

第二条命令同步导出 `requirements.txt`，供旧的 pip 部署使用。服务器上更新依赖需要 `uv sync --locked`，`/reload` 不会安装依赖。

---

## 常见问题

**启动时报「bot 或另一个备份/恢复进程正在运行」然后退出**
同一目录已有 bot 实例，或者命令行备份 / 恢复正在进行。先停掉旧进程。

**日志提示「未配置 LLM_API_KEY」**
`.env` 不存在或没填 `LLM_API_KEY`。注意 `.env` 必须放在项目根目录（和 `bot.py` 同级）。

**启动时报 `.env 第 N 行格式错误`**
该行不是 `KEY=VALUE` 格式。注释行要以 `#` 开头，值中含空格或 `#` 时用引号包起来。

**QQ 连不上 NapCat**
检查 NapCat WebUI 中的 WebSocket 服务端已启用、端口与 `NAPCAT_WS_URL` 一致、token 与 `.env` 的 `NAPCAT_TOKEN` 完全相同。

**QQ 能连上但 bot 不说话 / 发不出消息**
QQ 可能已被风控下线，而 NapCat 仍报告在线。主人私聊 `/probe` 查看，必要时 `/login` 获取二维码重新登录。

**群里不回复**
群聊需要 @bot、引用 bot、戳 bot 或以 `GROUP_TRIGGER` 开头；潜水接话还要求群号在 `.env` 的 `AMBIENT_GROUPS` 里。
模型也可能判断不需要接话，用 `/content <消息>` 可以看到这批消息会走哪条路径。

**飞书报「飞书未配置」**
`.env` 中缺少 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`。不用飞书就把它从 `PLATFORMS` 中去掉。

**飞书群里只有 @ 才有反应**
需要在开放平台申请「获取群组中所有消息」权限并发布新版本。

**主人命令提示「只有主人能用」**
回复里附带了你的 id，把它填进 `.env` 的 `MASTER_QQ` 或 `MASTER_FEISHU`，然后 `/restart`（首次需要在服务器上重启）。

**联网搜索不可用**
`LLM_BASE_URL` 不是 DeepSeek 官方地址时默认关闭，需在 `.env` 配 `DEEPSEEK_SEARCH_API_KEY`；也检查 `LLM_ENABLE_TOOLS`。

**改了配置没生效**
`config.py`、`config.local.py` 和 `.env` 只在启动时读取，需要 `/restart`；`/reload` 只重载插件和工具。
另外检查名字有没有拼错：启动日志和 `python3 configure.py show` 会提示 `config.local.py` 里不认识的配置项。

**`/update` 提示 config.py 有本地改动、拉取失败**
个人配置应写在 `config.local.py`，见[更新与升级](#更新与升级)里的迁移步骤。

---

## 安全与隐私

- **不要把密钥写进 `config.py` / `config.local.py` 或提交到 Git**，只写在 `.env` 或进程环境变量中。密钥一旦进入 Git 历史，即使后来删除也能被找回，应立即到对应平台更换。
- 主人命令可以执行 `git`、重启进程和 NapCat，`MASTER_QQ` / `MASTER_FEISHU` 务必填对。命令不加 `/` 也能匹配，
  主人私聊发「update」「restart」这样的单词同样会执行。
- 填了 `MASTER_QQ` 时，主人 QQ 号会写进人设提示词，随每次私聊和群聊请求发给模型服务；群聊时间线里也包含每位发言人的用户 ID。
- 长期记忆、表情库、通讯录全局共享；共享经历默认跨会话可见。多群部署时请评估隐私，必要时把 `AGENT_SHARE_EXPERIENCES` 设为 `False`。
- 备份 ZIP 未加密，包含密钥、私聊记录和 QQ 登录数据，权限为 `0600`，不要上传到公开位置。
- 聊天内容、引用原文、合并转发和图片中的文字都只作为材料交给模型，提示词明确它们不能改变规则或冒充身份；
  后台通知使用真人不可能拥有的发言人 ID `agent-notice`。
