# PromptQL Gateway

把 PromptQL 的 agent 能力包装成标准 API，附带账号池、自动注册、接码与管理后台。

> ## ⚠️ 免责声明 / DISCLAIMER
>
> **本项目仅供安全研究、自动化测试与学习交流使用。**
>
> - 使用者必须确保对目标服务的所有操作**符合其服务条款与当地法律法规**。
> - 本项目**不提供任何账号、密钥或凭据**，使用者需自备。
> - 因使用本项目产生的**任何后果（包括但不限于账号封禁、服务终止、法律风险）
>   由使用者自行承担**，作者不承担任何责任。
> - 请勿用于商业用途、批量滥用或任何未经授权的场景。
> - 若相关服务方要求停止，请立即停止使用并删除本项目。
>
> This project is for **authorized security research and educational purposes only**.
> Users are solely responsible for complying with all applicable laws and the
> terms of service of any third-party service. No accounts, keys, or credentials
> are provided. Use at your own risk.

---

## 平台限制

| 项 | 支持情况 |
|---|---|
| **注册（自动建号）** | ✅ **仅 Windows** —— 依赖 Camoufox 浏览器过 reCAPTCHA，实测在 Windows 可用 |
| **网关（调 API）** | ✅ 任意平台（Linux / macOS / Docker） |
| **MCP Server** | ✅ 任意平台 |
| **管理后台** | ✅ 任意平台（浏览器访问） |

> 注册环节需要真实浏览器指纹环境。Linux 上 `camoufox` 理论可用但**未实测**，
> 如需在 Linux 注册请自行验证。**只跑网关不注册的话，Linux 完全够用。**

## 它能做什么

| 能力 | 说明 |
|---|---|
| **三协议网关** | `/v1/chat/completions`（OpenAI）、`/v1/responses`、`/v1/messages`（Anthropic） |
| **账号池** | 多账号轮询、故障隔离、额度统计 |
| **自动注册** | 邮箱 OTP → 手机验证（自动接码）→ **自动加入有免费额度的公开项目** |
| **接码** | hero-sms / 5sim 可切换，单价硬闸（默认 $0.10），收不到码自动退单换号 |
| **本地工作区桥** | 让远程 agent 读写你本机的文件（网关做 I/O，agent 只负责"想"） |
| **MCP Server** | 暴露成 MCP 工具，供 Codex / ZCode / Claude Code 调用 |

## 快速开始

```bash
# 1. 装依赖
pip install -r requirements.txt

# 2. 启动
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

浏览器打开 `http://localhost:8080` 进管理后台。

**配合使用说明**

- **只想调 API**：装上依赖就能跑。在「下游 API」页建一个 Key，用
  `/v1/chat/completions` 调用即可。
- **需要自动注册**：额外装浏览器（**仅 Windows**）：
  ```bash
  pip install -r requirements-camoufox.txt
  python -m camoufox fetch
  ```
- **需要接码**：在「注册 → 接码配置」里选平台并粘贴密钥（会自动存到 `data/`）。

### 第一次使用的推荐流程

```
1. 启动服务 → 打开 http://localhost:8080
2. 「注册」页 → 接码配置 → 选平台 + 填密钥 → 保存 → 测试连通
3. 「注册」页 → 粘贴邮箱凭据（四段式）→ 开始注册
   （账号会自动加入带免费额度的公开项目）
4. 「下游 API」页 → 创建 Key
5. 用 Key 调 /v1/chat/completions
```

## 邮箱凭据格式

注册**必须提供**四段式凭据（不支持临时邮箱）：

```
email----password----refresh_token----client_id
```

- `password` 可用占位符 `x` 留空
- `refresh_token` 是 Microsoft OAuth refresh token（前缀 `M.C…`）
- `client_id` 是配对的 GUID

解析**不靠位置、靠形态**：第 1 段含 `@` 判为邮箱，末段是 GUID 判为 client_id，
其余按长度和前缀识别 refresh_token，剩下的当 password。

## 调用示例

```bash
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer <你的Key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude opus 5.5",
    "messages": [{"role":"user","content":"写一个 LRU 缓存"}]
  }'
```

可选模型取决账号所属项目（Playground 实测有 `claude opus 5.5` /
`claude fable 5.1` / `gpt-6.1 sol` / `gpt-6 astra`）。

**响应会多两个字段**：

- `reasoning_content` — agent 的思考过程
- `agent_tools` — 工具执行轨迹（`run_shell` / `write_file` / `run_program` …）

## 本地工作区桥

PromptQL 的 agent 有自己的沙箱，够不到你本机的文件。
但**网关就跑在你本机** —— 所以由网关替它做 I/O：

```
本地文件 ──读取──> 网关 ──拼进 prompt──> agent（只负责"想"）
                                          │
本地磁盘 <──写回── 网关 <──解析 <<<FILE:>>> ┘
```

```bash
python client/pql.py code src/app.py --task "重构并加类型注解" --write
```

**安全边界**：所有路径限制在 `WORKSPACE_ROOT` 内，拒绝 `..` 与绝对路径
（已实测拦截 4 类越界），写操作默认关闭。

## MCP Server

```json
{
  "mcpServers": {
    "promptql": {
      "command": "python",
      "args": ["mcp_server/server.py"],
      "cwd": "/path/to/promptql-gateway"
    }
  }
}
```

工具：`pql_ask` / `pql_code` / `pql_files` / `pql_read` / `pql_write` / `pql_pool`

## 配置

全部走环境变量，密钥也可在网页上填（会写到 `data/`）。

| 变量 | 默认 | 说明 |
|---|---|---|
| `APP_HOST` / `APP_PORT` | `0.0.0.0` / `8080` | 监听地址 |
| `PQL_PROXY` | *(空)* | 出口代理，**默认直连**。需要时填 `http://host:port` |
| `SMS_PROVIDER` | `hero` | 接码平台，`hero` 或 `5sim` |
| `SMS_MAX_PRICE` | `0.10` | 🔴 单号单价硬闸（美元），超价拒单 |
| `SMS_MAX_TRIES` | `3` | 收不到码最多换几个号（每次先退单） |
| `SMS_TOTAL_BUDGET` | `0.30` | 单次注册接码总预算 |
| `MAIL_CHANNEL` | `outlook` | 只支持 outlook（RT 凭据） |
| `WORKSPACE_ROOT` | `data/workspace` | 本地工作区根目录（路径沙箱） |
| `WORKSPACE_ALLOW_WRITE` | `false` | 是否允许 agent 写回本机文件 |

> ⚠️ **`PQL_PROXY` 的一个坑**：代码在「不用代理」时显式传
> `{http: None, https: None, all: None}`，而不是 `None`。
> 因为 `requests` 对 `proxies=None` 会**回退到环境变量** ——
> 机器上别的程序注入的 `HTTP_PROXY` 会把请求截走，
> 表现为「一会儿通一会儿超时」，极难排查。

## 部署

见 `deploy/deploy.md`。Docker：

```bash
mkdir -p data && echo "<接码密钥>" > data/hero_sms.key
docker compose up -d --build
```

**反向代理必须关缓冲**（否则流式响应会被攒到最后一次性吐出）：

```nginx
proxy_buffering off;
proxy_read_timeout 600s;
```

## 数据与密钥

```
data/
  master.key       ⚠️ 加密主密钥（首次启动自动生成），务必备份
  hero_sms.key     接码密钥
  app.db           账号池 / 用量 / 下游 Key / 日志
  profiles/        每账号一个浏览器指纹档案
  workspace/       本地工作区（agent 读写沙箱）
```

整个 `data/` 在 `.gitignore` 里，**不会进版本库**。

## 已知限制

- **不能当 Claude Code 的模型后端**：PromptQL 是封闭 agent，不接受外部工具定义，
  不会返回 `tool_use`。要接入请用 **MCP Server**。
- **每次调用有固定开销**：agent 的 system prompt 约 40~60k tokens（缓存命中率高）。
- **模型按项目可用**：不同项目可见的模型不同，网关按账号所属项目解析。
- **注册需要浏览器**：仅 Windows 实测可用。

## License

仅供学习研究，未提供任何担保。
