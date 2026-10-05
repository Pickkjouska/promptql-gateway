# PromptQL 反代网关 —— Linux 部署

## 一、最快路径（Docker）

```bash
tar -xzf promptql-gateway-*.tar.gz && cd promptql-gateway-*
mkdir -p data
echo "<你的hero-sms_Token>" > data/hero_sms.key   # 手机验证需要；不给则跳过接码
docker compose up -d --build
curl -s localhost:8080/healthz
```

浏览器打开 `http://<主机>:8080/` 即控制台。

## 二、裸机部署（systemd）

```bash
sudo useradd -r -s /usr/sbin/nologin promptql
sudo mkdir -p /opt/promptql-gateway && sudo chown promptql: /opt/promptql-gateway
tar -xzf promptql-gateway-*.tar.gz --strip-components=1 -C /opt/promptql-gateway

cd /opt/promptql-gateway
sudo -u promptql python3 -m venv .venv
sudo -u promptql .venv/bin/pip install -r requirements.txt
# 注册环节需要浏览器（过 reCAPTCHA），如要自动注册再装：
sudo -u promptql .venv/bin/pip install -r requirements-camoufox.txt
sudo -u promptql .venv/bin/python -m camoufox fetch

sudo mkdir -p /opt/promptql-gateway/data
sudo -u promptql bash -c 'echo "<hero-sms_Token>" > /opt/promptql-gateway/data/hero_sms.key'

sudo cp deploy/promptql-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now promptql-gateway
sudo systemctl status promptql-gateway
```

## 三、密钥投放（三选一，优先级从高到低）

| 方式 | 做法 | 适用 |
|---|---|---|
| 环境变量 | `HERO_SMS=<token>` | Docker / systemd `EnvironmentFile` |
| secrets.json | `data/secrets.json` 里 `{"hero_sms":"<token>"}` | 多密钥集中管理 |
| 单文件 | `data/hero_sms.key` | 最省事 |

**主密钥** `APP_MASTER_KEY`：用于加密库里的邮箱 refresh_token 与 cookie。
不设置会在 `data/master.key` 自动生成 —— **务必持久化该文件**，删了旧凭据就解不开了。

## 四、反向代理（公网必需）

见 `deploy/nginx.conf.example`。关键点：

- `proxy_buffering off` —— **不关会导致流式响应被攒到最后一次性吐出**
- `proxy_read_timeout 600s` —— 上游 agent 思考可能几分钟

## 五、部署后自检

```bash
bash scripts/smoke_test.sh
SMOKE_KEY=<下游Key> bash scripts/smoke_test.sh     # 连网关一起测（消耗额度）
```

## 六、目录与数据

```
data/
  app.db         SQLite（账号/用量/日志/下游Key）
  master.key     ⚠️ 加密主密钥，必须备份
  hero_sms.key   hero-sms Token
  secrets.json   可选，集中密钥
  profiles/      每账号一个浏览器指纹档案（注册用）
```

备份：`tar czf backup.tgz -C /opt/promptql-gateway data`

## 七、常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 流式不流 | nginx 开了 buffering | 加 `proxy_buffering off` |
| 账号 status=cookie_expired | hasura-lux 失效（约 2 个月） | 重新导入该账号凭据并注册一次 |
| 手机验证失败 | hero-sms 无库存 / 超价被闸 / 上游限流 | 看日志页 events；换国家或补余额 |
| 调用返回 402 | **上游要求绑卡** | 系统已熔断该账号，需人工处理 |
| 注册卡在 captcha | 未装 Camoufox | 装 `requirements-camoufox.txt` 并 `camoufox fetch` |


## 八、接码与邮箱配置（实测默认值）

```bash
SMS_PROVIDER=hero              # hero | 5sim
SMS_MAX_PRICE=0.10             # 🔴 单号单价硬闸（美元）
SMS_MAX_TRIES=2                # 收不到码最多换几个号（会先退单）
SMS_TOTAL_BUDGET=0.20          # 单次注册的接码总预算上限
HERO_SMS_COUNTRY=33            # 33 = 哥伦比亚
HERO_SMS_SERVICE=ot            # ot = other（通用验证码）

MAIL_CHANNEL=outlook           # 只支持 outlook（RT 凭据），不支持临时邮箱
```

**邮箱通道说明**

只支持 **outlook**（refresh-token 凭据）：
```
email----password----refresh_token----client_id
```
必须提供凭据才能注册 —— 不提供临时邮箱（共享域名易被上游风控识别）。

**注册流程实测耗时**（单账号，含手机验证）：

| 阶段 | 耗时 |
|---|---|
| 建临时邮箱 | ~1s |
| 起浏览器 | ~4s |
| 打开登录页 | ~7s |
| reCAPTCHA 就绪 | ~18s |
| 发信 + 收码 | ~11s |
| 提交 OTP | ~10s |
| hero-sms 取号 + 收码 + 提交 | ~14s |
| **合计** | **~67s** |
