# -*- coding: utf-8 -*-
"""
config.py —— 集中配置（环境变量优先，可覆盖）

不写死任何凭据。密钥来源优先级:
  环境变量  >  data/secrets.json  >  data/*.key 文件
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
SECRETS_JSON = DATA / "secrets.json"

# ---- 上游 PromptQL ----
PROJECT_ID = os.getenv("PQL_PROJECT_ID", "4c2a0298-30c6-4b30-84d8-8440f5b356ee")
HGE = os.getenv("PQL_HGE",
                "https://data.prompt.ql.app/promptql/playground-v2-hge/v1/graphql")
AUTH_HOST = os.getenv("PQL_AUTH", "https://auth.pro.ql.app")
CONTROL_PLANE = os.getenv("PQL_CONTROL_PLANE", "https://data.pro.ql.app/v1/graphql")
SITE = os.getenv("PQL_SITE", "https://prompt.ql.app")
PROJECT_NAME = os.getenv("PQL_PROJECT_NAME", "promptql-community")

# ---- 模型目录（实测得到） ----
MODELS = {
    "claude-fable-5-1": "5444b356-874d-4742-829b-ad0924b8df18",
    "claude-opus-5-5": "703dbedc-136a-40da-8158-9116adc463ee",
    "gpt-6.1-sol": "8b028746-86fd-487d-a02b-7322c5d72073",
    "gpt-6-astra": "ddf1b6be-a9e2-4c37-b44f-9805c5b69576",
}
DEFAULT_MODEL = os.getenv("PQL_DEFAULT_MODEL", "claude-fable-5-1")

# ---- 网络 ----
# ---- 出口代理（可选，默认关闭）----
#
# 默认**不使用代理**（直连）。需要时用环境变量开启：
#
#     PQL_PROXY=http://127.0.0.1:<port>
#
# ⚠️ 注意 requests 的一个陷阱：
#     proxies=None 会**回退到环境变量**（HTTP_PROXY / HTTPS_PROXY）。
#     如果机器上有别的程序注入了代理变量，请求可能被意外截走 ——
#     表现为「一会儿通一会儿超时」。所以「不用代理」时会显式传
#     {http: None, https: None, all: None} 来绕过所有代理。
_new_proxy = os.getenv("PQL_PROXY", "").strip()
PROXY_URL = "" if _new_proxy.lower() in ("none", "direct", "off", "") else _new_proxy
HTTP_TIMEOUT = int(os.getenv("PQL_HTTP_TIMEOUT", "60"))
# 单次调用最长等待。上游 agent 做复杂任务可能 5~10 分钟，默认给 600s。
LLM_WAIT_S = int(os.getenv("PQL_LLM_WAIT", "600"))
LLM_POLL_S = float(os.getenv("PQL_LLM_POLL", "1.5"))

# ---- 接码提供商选择 ----
# 可选: "hero"（默认，推荐）| "5sim"
# 只用 hero-sms（用户指定）。5sim 代码保留但不再默认。
def _provider_file():
    return DATA / "sms_provider.txt"


def sms_provider() -> str:
    """
    当前接码商。优先级: 本地文件 > 环境变量 > 默认 hero。

    做成"本地文件可覆盖"是为了让网页上能直接切换，无需重启服务。
    """
    f = _provider_file()
    if f.exists():
        v = f.read_text(encoding="utf-8").strip().lower()
        if v in ("hero", "5sim"):
            return v
    v = os.getenv("SMS_PROVIDER", "hero").strip().lower()
    return v if v in ("hero", "5sim") else "hero"


def set_sms_provider(name: str) -> str:
    name = (name or "").strip().lower()
    if name not in ("hero", "5sim"):
        raise ValueError(f"不支持的接码商: {name!r}（只支持 hero / 5sim）")
    DATA.mkdir(parents=True, exist_ok=True)
    _provider_file().write_text(name, encoding="utf-8")
    return name


SMS_PROVIDER = os.getenv("SMS_PROVIDER", "hero").strip().lower()

# 🔴 共用硬闸: 单号单价上限(美元)。两个提供商都受此约束。
SMS_MAX_PRICE = float(os.getenv("SMS_MAX_PRICE", "0.10"))
# 收不到码时最多换几个号（每次都会先退掉旧单）
SMS_MAX_TRIES = int(os.getenv("SMS_MAX_TRIES", "3"))   # 一账号一码，串行换号最多 3 次
# 单次注册的接码总预算闸（美元）。超出即停，不再换号。
SMS_TOTAL_BUDGET = float(os.getenv("SMS_TOTAL_BUDGET", "0.30"))  # 3 次 × $0.048 足够
# 单个号等码超时（秒）。太长会把任务拖住，200s 足够。
SMS_WAIT_S = int(os.getenv("SMS_WAIT_S", "200"))

# ---- hero-sms（默认提供商） ----
HERO_SMS_BASE = os.getenv("HERO_SMS_BASE", "https://hero-sms.com/stubs/handler_api.php")
HERO_SMS_COUNTRY = os.getenv("HERO_SMS_COUNTRY", "33")        # 33 = Colombia
HERO_SMS_SERVICE = os.getenv("HERO_SMS_SERVICE", "ot")        # ot = other
HERO_SMS_OPERATOR = os.getenv("HERO_SMS_OPERATOR", "")        # 空 = 自动选最便宜
# hero-sms 最小激活期实测 = 120s，未满退款会被 EARLY_CANCEL_DENIED 拒绝。
# True = 换号前等满再退款（钱能退回来，每轮多等 ~120s）
HERO_SETTLE_BEFORE_RETRY = os.getenv("HERO_SETTLE_BEFORE_RETRY", "true").lower() != "false"

# ---- 5sim（备选） ----
FIVESIM_BASE = os.getenv("FIVESIM_BASE", "https://5sim.net")
FIVESIM_MAX_PRICE = SMS_MAX_PRICE
FIVESIM_COUNTRY = os.getenv("FIVESIM_COUNTRY", "england")
FIVESIM_OPERATOR = os.getenv("FIVESIM_OPERATOR", "")
FIVESIM_PRODUCT = os.getenv("FIVESIM_PRODUCT", "other")
FIVESIM_WAIT_S = SMS_WAIT_S
FIVESIM_MAX_TRIES = SMS_MAX_TRIES
FIVESIM_TOTAL_BUDGET = SMS_TOTAL_BUDGET

# ---- 邮箱 ----
# 🔴 只支持 outlook（RT 凭据）。不支持临时邮箱 —— 共享域名易被风控识别。
#    必须提供四段式凭据 email----password----refresh_token----client_id。
MAIL_CHANNEL = os.getenv("MAIL_CHANNEL", "outlook").strip().lower()
# outlook（备用通道）
GRAPH_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
OTP_SENDER_HINT = os.getenv("OTP_SENDER_HINT", "auth.promptql.app")

# ---- 服务 ----
HOST = os.getenv("APP_HOST", "0.0.0.0")
PORT = int(os.getenv("APP_PORT", "8080"))
# 主密钥用于加密 DB 里的凭据；未设置则落到 data/master.key
MASTER_KEY = os.getenv("APP_MASTER_KEY", "").strip()

# ---- 本地工作区桥（让远程 agent 读写本机文件）----
WORKSPACE_ROOT = os.getenv("WORKSPACE_ROOT", str(DATA / "workspace"))
WORKSPACE_MAX_READ = int(os.getenv("WORKSPACE_MAX_READ", "200000"))     # 单文件读取上限
WORKSPACE_MAX_TOTAL = int(os.getenv("WORKSPACE_MAX_TOTAL", "400000"))   # 拼进 prompt 的总上限
# 🔴 默认禁止写回本机（安全）。要用时在请求里带 write=true 或开这个开关。
WORKSPACE_ALLOW_WRITE = os.getenv("WORKSPACE_ALLOW_WRITE", "false").lower() == "true"

# ---- 裂变 ----
# 🔴 实测：裂变奖励($25)必须先绑卡才解锁，所以默认**不在注册时自动裂变**。
#    开启后系统会尝试邀请，遇到绑卡要求立即熔断上报，绝不自动绑卡。
INVITE_AT_REGISTER = os.getenv("INVITE_AT_REGISTER", "false").lower() == "true"

# ---- 注册纪律 ----
# 同出口最短复用 / 账号间隔（沿用项目规范）
MIN_EGRESS_REUSE_S = int(os.getenv("MIN_EGRESS_REUSE_S", "300"))
ACCOUNT_GAP_S = (int(os.getenv("ACCOUNT_GAP_MIN", "8")), int(os.getenv("ACCOUNT_GAP_MAX", "15")))

# 🔴 银行卡/支付熔断关键词：命中即中止该账号并上报，禁止盲目重试
PAYMENT_TRIPWIRES = (
    "card", "credit card", "payment method", "billing", "add a card",
    "银行卡", "信用卡", "支付方式", "绑定支付", "purchase", "subscribe",
    "upgrade to", "requires a card", "stripe", "checkout",
)


def load_secret(name: str, default: str = "") -> str:
    """三级回退: 环境变量 > secrets.json > <name>.key 文件"""
    env = os.getenv(name.upper(), "").strip()
    if env:
        return env
    if SECRETS_JSON.exists():
        try:
            d = json.loads(SECRETS_JSON.read_text(encoding="utf-8"))
            if d.get(name):
                return str(d[name]).strip()
        except Exception:
            pass
    f = DATA / f"{name}.key"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    return default


def save_secret(name: str, value: str) -> str:
    """
    把密钥写到 data/<name>.key（本地文件，权限 0600）。

    🔴 安全约定:
      - data/ 整个目录在 .gitignore 里，**永不进版本库**
      - 文件权限设为 0600（仅属主可读写），Linux 下生效
      - 只在本地落盘，任何情况下不通过 API 回传明文

    返回写入的文件路径。
    """
    DATA.mkdir(parents=True, exist_ok=True)
    f = DATA / f"{name}.key"
    f.write_text((value or "").strip(), encoding="utf-8")
    try:
        f.chmod(0o600)
    except Exception:
        pass          # Windows 下 chmod 基本无效，忽略
    return str(f)


def mask_secret(name: str) -> str:
    """
    给前端看的状态：**只回传「已配置 + 长度」**，绝不暴露任何真实字符。

    🔴 之前用 `前4位…后4位` 的掩码，仍会泄漏 8 个真实字符。
       对 JWT 类密钥（base64 头固定）尤其危险 —— 前 4 位几乎总是 `eyJh`，
       等于白送。现在改成固定长度的纯掩码，只作为「填没填」的指示。
    """
    v = load_secret(name)
    if not v:
        return ""
    n = len(v)
    shown = min(max(n // 8, 4), 12)      # 4~12 个点，仅表示"有值"
    return "•" * shown + f"  （{n} 位）"


def fivesim_key() -> str:
    return load_secret("fivesim")


def hero_key() -> str:
    return load_secret("hero_sms") or load_secret("herosms")


_NO_PROXY = {"http": None, "https": None, "all": None}


def proxy_dict() -> dict:
    """
    requests 的 proxies 参数。

    🔴 关键坑: 未配置代理时必须返回
       {"http": None, "https": None, "all": None} 而**不是 None**。

       因为 requests 对 proxies=None 的行为是「回退到环境变量」——
       如果运行环境里存在 HTTP_PROXY / HTTPS_PROXY，
       请求会被那个代理截走，连不上上游就干等 →
       表现为「连接不顺畅 / 一会儿通一会儿超时」。

       显式给 None 才是真正的「绕过所有代理」。
    """
    if not PROXY_URL:
        return dict(_NO_PROXY)          # 仅在显式要求直连时走到这
    return {"http": PROXY_URL, "https": PROXY_URL, "all": PROXY_URL}
