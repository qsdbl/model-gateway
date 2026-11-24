# Model Gateway

一个 OpenAI 兼容的模型网关：对外只暴露两个模型名（`low` / `high`），内部在「账号 × 模型」候选池里自动调度——免费优先、轮转、失败冷却、付费兜底。带一个单文件 Web 管理后台：扫码加账号、看用量、切模型、配邮件。

> **定位说明**：本项目是一个**网关框架 + 调度器**。它本身不提供模型，也不带任何账号或积分。要真实推理，需要你自己准备上游账号（见下文「准备上游」）。仓库默认 `GATEWAY_UPSTREAM=mock`，可以直接启动看界面，但不会真实调用模型。

---

## 功能

- **OpenAI 兼容**：`/v1/chat/completions`（流式 + 非流式）、`/v1/responses`、`/v1/models`
- **两个对外模型名**：`low`（免费优先的简单任务）、`high`（走指定的强模型）
- **自动调度**：置顶模型 > 平台顺序 > 本次是否免费 > 预计扣费升序 > 滑动窗口轮转 > 失败冷却
- **多账号池**：扫码或导入凭据添加多个账号，自动轮转、失败换号
- **自建签到**：网关自己按日签到攒积分（WorkBuddy），凭据只存本地 `credentials/`（0600）
- **管理后台**：账号、模型、调度、用量、记录、安全、接入配置，5 秒实时刷新
- **邮件**：找回登录口令、测试邮件、提醒邮件；SMTP 可在后台「安全」页配置（存 SQLite）
- **SQLite 存储**：单文件数据库，自动建表，零外部数据库依赖

---

## 架构

```
客户端 (OpenAI SDK / Codex / WorkBuddy / 浏览器)
        │  /v1/chat/completions  Bearer <API_KEY>
        ▼
   Model Gateway (FastAPI, :8080)
        │  调度器：账号×模型候选池排序
        ├──► WorkBuddy 上游 (HTTPS API + codebuddy CLI 扫码/签到)
        └──► Qoder 上游 (qoderclicn 子进程)
        │
        ▼
   SQLite (data/gateway.db)  +  credentials/ (0600)
```

所有路径默认相对项目根，配置全部走环境变量（见 `.env.example`）。数据库首次启动自动建表，无需 migration。

---

## 快速开始

### 1. 环境要求

- Python 3.10+
- 无需 Node.js（两个 CLI 都可用原生/自带方式安装）

### 2. 安装 Python 依赖

```bash
cd model-gateway
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

（`requirements.txt` 含运行依赖 `fastapi / uvicorn / segno`——其中 `segno` 用于扫码登录，漏装会导致扫码报错。测试另需 `pip install pytest httpx`。）

### 3. 配置

```bash
cp .env.example .env
```

至少设置管理口令，否则后台会 503：

```
GATEWAY_ADMIN_TOKEN=你自己的一串随机口令
```

想让客户端调 `/v1/*`，再设 API Key：

```
GATEWAY_API_KEY=另一串随机key
```

### 4. 启动

```bash
source .venv/bin/activate
uvicorn gateway.app:app --host 127.0.0.1 --port 8080
```

打开 `http://127.0.0.1:8080/admin`，输入 `GATEWAY_ADMIN_TOKEN` 登录。

此时是 `mock` 上游：界面、后台、模型列表都能看，但不会真实调用模型。要真实使用，继续下面的「准备上游」。

---

## 准备上游（真实推理必需）

网关支持两个商业上游。它们的账号和 CLI 都需要你自己准备——**本项目不分发任何二进制，也不附带任何账号**。

### WorkBuddy

需要两样东西：

**① 安装 `codebuddy` CLI**（用于扫码登录与自动签到）

官方原生安装器（无需 Node.js，推荐，与本网关开发环境一致）：

```bash
curl -fsSL https://copilot.tencent.com/cli/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"   # 若安装脚本未自动配置
codebuddy --version                    # 验证
```

也可以用 npm（需 Node.js 18.20+）：

```bash
npm install -g @tencent-ai/codebuddy-code
```

安装文档：https://www.codebuddy.cn/docs/cli/installation

**② 在后台扫码添加账号**

后台 →「账号」→「添加账号」→「扫码登录」，用手机扫 CLI 打印的二维码。登录成功后凭据自动写入本地 `credentials/`（0600）并登记入库。

也可以直接「粘贴 .info」导入你已有的明文凭据。

### Qoder

**① 安装官方 SDK（自带 `qoderclicn`）**

```bash
pip install qodercn-agent-sdk
```

这个 PyPI 包自带一个 183MB 的 `qoderclicn` 可执行文件（node-sea 单文件，不需要 Node）。装完用 `which qoderclicn` 确认在 PATH 里。

**② 在后台扫码授权**

后台 →「账号」→「添加账号」→ Qoder 扫码授权。每个账号需各自完成一次设备码授权（约 5 分钟有效期，页面会自动换链接）。授权成功后自动落盘凭据、登记入库、刷新余额。

> 注意：两个 CLI 都不随本仓库分发，请从上述官方渠道安装。路径不在 PATH 时，可用 `GATEWAY_CLI_BIN` / `GATEWAY_QODER_CLI` 显式指定。

---

## 免费与积分（重要概念）

- **官方免费模型**（如 `hy4flash-free`）：不消耗付费积分，**账号余额为 0 也能用**。
- **短请求免费档**：某些付费模型在极短输入时实测扣 0，但它们本质是付费模型，账号余额为 0 时会被调度器剔除。
- **积分**：由上游平台发放并管理（签到、新号赠送等），网关只能读取余额、不能凭空产生积分。某些平台的新账号积分需要你本人在官方客户端首次登录后领取。

调度器按「本次请求的实际预估成本」选路，免费与否取决于**本次 token 长度 × 模型价格曲线**，不是「某个模型永远免费 / 永远收费」的二分。

---

## 客户端接入

### 通用 OpenAI SDK

```python
from openai import OpenAI
client = OpenAI(api_key="你的 GATEWAY_API_KEY", base_url="http://127.0.0.1:8080/v1")
resp = client.chat.completions.create(
    model="low",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

### Codex CLI（wire_api = responses）

在配置里加一个 provider：

```toml
[model_providers.gateway]
name = "gateway"
base_url = "http://127.0.0.1:8080/v1"
wire_api = "responses"
requires_openai_auth = false
```

### WorkBuddy 自定义模型

`~/.workbuddy/models.json` 里加一条，URL 指向 `http://127.0.0.1:8080/v1/chat/completions`，API Key 填 `GATEWAY_API_KEY`，模型 ID 填 `low` 或 `high`。

更多可复制的配置模板见 `docs/examples/`。后台「接入」页也会按当前域名自动生成配置块。

---

## 邮件配置

后台 →「安全」→「SMTP 配置」填写：

- SMTP 主机 / 端口（QQ 邮箱 465 勾选 SSL；587 不勾选）
- 发信账号（邮箱地址）
- 授权码（邮箱「SMTP 授权码」，不是登录密码；**留空表示不修改**）
- 发件显示地址（留空同发信账号）

配置存 SQLite，页面只显示「是否已设置」，不回显授权码。也可用 `GATEWAY_SMTP_*` 环境变量兜底（见 `.env.example`）。填完点「发一封测试邮件」验证。

---

## 部署到服务器（生产）

本仓库只含应用代码，不含任何部署脚本（部署环境与个人服务器强相关，请按自己的环境配置）。要点：

1. 应用只监听 `127.0.0.1:8080`，**用 nginx/Caddy 反代 + HTTPS 暴露**，不要裸绑 `0.0.0.0` 明文 HTTP。
2. `.env` 权限 600，绝不进 git。
3. 反代把 `/gw/` 指到 `http://127.0.0.1:8080/`，客户端 base URL 形如 `https://你的域名/gw/v1`。
4. 用 systemd / supervisor 托管 `uvicorn gateway.app:app`。
5. 凭据目录 `credentials/` 权限 700、文件 600。

后台会话、登录限流、邮件找回等安全机制已内置，配置见 `.env.example`。

---

## 目录结构

```
gateway/          核心代码（FastAPI app / 调度 / 上游适配 / 存储 / 后台 API）
admin/            单文件 Web 后台（index.html）+ 本地 ECharts
adapters/         上游适配器扩展位
docs/examples/    客户端配置模板
data/             运行时数据（SQLite/凭据/路由配置，gitignore）
```

---

## 许可

本项目代码采用 MIT License（见 `LICENSE`）。

`admin/vendor/echarts.min.js` 来自 Apache ECharts，采用 Apache License 2.0（见 `admin/vendor/LICENSE`），版权归原作者所有。

---

## 免责声明

本项目仅供个人学习与研究使用。接入的上游平台（WorkBuddy、Qoder 等）均为第三方商业服务，其账号体系、积分规则与用户协议由各自平台制定。使用本项目时请遵守相应平台的用户协议与服务条款；因使用本项目产生的账号、额度或合规问题由使用者自行承担。本项目不提供任何账号、积分或二进制文件，也不代表上述平台的官方立场。
