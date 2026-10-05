# -*- coding: utf-8 -*-
"""
promptql.py —— PromptQL 上游协议客户端（免浏览器）

认证链（全部实测通过）:
    ① cookie(hasura-lux) --> POST {AUTH}/ddn/promptql/token  --> luxJWT   [24h]
    ② luxJWT             --> mutation enrich_token           --> UDJWT    [24h]
    ③ UDJWT(Bearer)      --> HGE 上的约定 / mutation

关键事实:
  - 控制面(data.pro) 用 luxJWT；数据面(data.prompt) 用 UDJWT
  - 提交/订阅走同一个 HGE 端点，线程消息靠 subscription thread_events_stream
  - 模型切换: start_thread 带 llmConfigId
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field

import requests

from .. import config
from .http import http


class UpstreamError(RuntimeError):
    pass


class AuthExpired(UpstreamError):
    """cookie 失效，需要重新登录取 cookie。"""


class PaymentRequired(UpstreamError):
    """上游要求绑卡/付费 —— 熔断信号，禁止重试。"""


def _b64d(s: str) -> dict:
    s += "=" * (-len(s) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(s))
    except Exception:
        return {}


def jwt_exp(tok: str) -> int:
    parts = tok.split(".")
    if len(parts) != 3:
        return 0
    return int(_b64d(parts[1]).get("exp") or 0)


@dataclass
class Session:
    """
    一个账号的上游会话（luxJWT + UDJWT + 身份 id）。

    🔴 project_id 是**每账号**的，不是全局常量：
       enrich_token(luxJWT, projectId) 会校验该用户是否真的能访问该项目，
       拿别人的项目 id 去打会得到 PROJECT_ACCESS_DENIED。
       所以新注册的账号要先 ddnCreatePromptQLProject 建自己的项目。
    """
    cookie: str
    lux_jwt: str = ""
    udjwt: str = ""
    lux_exp: int = 0
    udjwt_exp: int = 0
    pql_user_id: str = ""
    ctrl_user_id: str = ""
    email: str = ""
    project_id: str = ""            # 空则回退到 config.PROJECT_ID
    balance_usd: float | None = None
    info: dict = field(default_factory=dict)

    @property
    def project(self) -> str:
        return self.project_id or config.PROJECT_ID

    def _expired(self, exp: int) -> bool:
        return (not exp) or exp - time.time() < 300

    def ensure(self, force: bool = False):
        if force or not self.lux_jwt or self._expired(self.lux_exp):
            self.mint_lux()
        if not self.project_id:
            created = False
            try:
                before = self.project_id
                ensure_project(self)
                created = bool(self.project_id) and self.project_id != before
            except Exception:
                pass          # 建不了就回退到 config.PROJECT_ID
            # 🔴 项目是**刚建**的 → 刚才那份 luxJWT 里不含它的 user-access，
            #    直接拿去 enrich_token 会被拒: "User does not have access to project"。
            #    必须重签一次，让 token 带上新项目的权限。
            if created:
                self.mint_lux()
        if force or not self.udjwt or self._expired(self.udjwt_exp):
            self.mint_udjwt()
        return self

    # ---- ① luxJWT ----
    def mint_lux(self):
        r = http.post(f"{config.AUTH_HOST}/ddn/promptql/token",
                          headers={"cookie": f"hasura-lux={self.cookie}",
                                   "accept": "*/*"},
                          proxies=config.proxy_dict(), timeout=config.HTTP_TIMEOUT)
        if r.status_code == 401:
            raise AuthExpired(f"cookie 失效: {r.text[:160]}")
        if r.status_code != 200:
            raise UpstreamError(f"换 luxJWT 失败 HTTP {r.status_code}: {r.text[:200]}")
        j = r.json()
        if j.get("status") != "success" or not j.get("token"):
            raise AuthExpired(f"换 luxJWT 未成功: {json.dumps(j)[:200]}")
        self.lux_jwt = j["token"]
        self.lux_exp = jwt_exp(self.lux_jwt)
        p = _b64d(self.lux_jwt.split(".")[1])
        ns = p.get("https://promptql.hasura.io", {}) or {}
        self.ctrl_user_id = self.ctrl_user_id or str(p.get("sub") or "")
        ui = ns.get("user-info") or {}
        self.email = self.email or str(ui.get("email") or "")
        self.info["lux"] = {"exp": self.lux_exp, "accesses": ns.get("user-accesses")}
        return self.lux_jwt

    # ---- ② UDJWT ----
    def mint_udjwt(self):
        if not self.lux_jwt:
            self.mint_lux()
        q = """mutation EnrichToken($luxJWT: String!, $projectId: uuid!) {
          enrich_token(luxJWT: $luxJWT, projectId: $projectId) { userDirectoryJWT }
        }"""
        st, body = self.gql(config.HGE, q, {"luxJWT": self.lux_jwt,
                                            "projectId": self.project},
                            bearer=None, cookie=True)
        u = (((body or {}).get("data") or {}).get("enrich_token") or {}).get("userDirectoryJWT")
        if not u:
            raise AuthExpired(f"enrich_token 失败: {json.dumps(body, ensure_ascii=False)[:240]}")
        self.udjwt = u
        self.udjwt_exp = jwt_exp(u)
        p = _b64d(u.split(".")[1]).get("https://promptql.hasura.io", {}) or {}
        self.pql_user_id = str(p.get("x-hasura-promptql-user-id") or "")
        self.ctrl_user_id = str(p.get("x-hasura-control-plane-user-id") or self.ctrl_user_id)
        self.email = str(p.get("x-hasura-email") or self.email)
        self.info["udjwt"] = {"exp": self.udjwt_exp, "pql_user_id": self.pql_user_id}
        return self.udjwt

    # ---- 通用 GraphQL ----
    def gql(self, url: str, query: str, variables=None, *, bearer: str | None = "__auto__",
            cookie: bool = False, timeout=None):
        h = {"content-type": "application/json", "accept": "application/json"}
        if bearer == "__auto__":
            self.ensure()
            h["authorization"] = f"Bearer {self.udjwt}"
        elif bearer:
            h["authorization"] = f"Bearer {bearer}"
        if cookie:
            h["cookie"] = f"hasura-lux={self.cookie}"
        r = http.post(url, headers=h,
                          json={"query": query, "variables": variables or {}},
                          proxies=config.proxy_dict(),
                          timeout=timeout or config.HTTP_TIMEOUT)
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:400]}
        _tripwire(body)
        if "Authentication hook unauthorized" in json.dumps(body):
            # 正常路径不会走到这（已带 UDJWT）；说明 token 类型用错
            raise AuthExpired("认证被拒：token 类型不匹配或已失效")
        return r.status_code, body

    # ---- 业务 ----
    def whoami(self) -> dict:
        st, b = self.gql(config.CONTROL_PLANE,
                         "query getCurrentUser { users { id email customer_id created_at } }",
                         bearer=None, cookie=True)
        if st == 200 and "data" in b:
            us = b["data"].get("users") or []
            if us:
                self.ctrl_user_id = us[0]["id"]
                self.email = us[0]["email"]
                return us[0]
        return {}

    def models(self) -> list[dict]:
        st, b = self.gql(config.HGE,
                         "query AllLlm { llm_config(order_by:{display_label:asc}) "
                         "{ id display_label deleted_at } }")
        return ((b or {}).get("data") or {}).get("llm_config") or []

    def entitlements(self) -> list[dict]:
        st, b = self.gql(config.CONTROL_PLANE,
                         """query E($userId: uuid!) {
                              user_entitlement_access(where:{user_id:{_eq:$userId}}) {
                                entitlement { name config_limit config_is_enabled type } } }""",
                         {"userId": self.ctrl_user_id or self._ctrl_from_lux()},
                         bearer=None, cookie=True)
        return (((b or {}).get("data") or {}).get("user_entitlement_access")) or []

    def _ctrl_from_lux(self) -> str:
        if not self.lux_jwt:
            self.mint_lux()
        return str(_b64d(self.lux_jwt.split(".")[1]).get("sub") or "")

    def olu(self) -> dict:
        """免费额度: granted / used（单位 OLU）。"""
        uid = self.pql_user_id or self.info.get("udjwt", {}).get("pql_user_id")
        if not uid:
            self.ensure()
            uid = self.pql_user_id
        st, b = self.gql(config.HGE, """query P($promptql_user_id: uuid!) {
              personal_olu_status(args:{promptql_user_id:$promptql_user_id}) {
                granted_olus used_olus } }""", {"promptql_user_id": uid})
        rows = ((b or {}).get("data") or {}).get("personal_olu_status") or []
        if rows:
            return {"granted": float(rows[0].get("granted_olus") or 0),
                    "used": float(rows[0].get("used_olus") or 0)}
        return {}

    _catalog: dict = field(default=None, repr=False, compare=False)
    _mounted: list = field(default=None, repr=False, compare=False)

    def mounted_models(self) -> list[dict]:
        """
        **当前项目真正挂载**的模型。

        🔴 实测教训: 只解析到 llm_config id 是不够的 —— 发一个未挂载的 id 给
           start_thread，agent 不报错，而是**静默返回 `<done />` 空回复**
           （耗时还很久）。所以选模型必须**以挂载列表为准**。
        """
        if getattr(self, "_mounted", None) is not None:
            return self._mounted
        out = []
        try:
            st, b = self.gql(config.HGE, """query {
              project_llm {
                llm_config { id display_label deleted_at }
                default_llm_config { id display_label deleted_at }
              } }""")
            for r in ((((b or {}).get("data") or {}).get("project_llm")) or []):
                for k in ("llm_config", "default_llm_config"):
                    c = r.get(k) or {}
                    if c.get("id") and not c.get("deleted_at"):
                        out.append({"id": c["id"],
                                    "label": c.get("display_label") or "",
                                    "default": (k == "default_llm_config")})
        except Exception:
            pass
        self._mounted = out
        return out

    def model_catalog(self) -> dict:
        """
        可切换的模型目录。

        🔴 实测修正: 用**全局 llm_config 目录**，不用 project_llm。

           原因: Playground 的 `project_llm` 只返回 1 个模型（GPT-6.1 Sol），
           但直接传 `Claude Opus 5.5` 的 id **完全可用**（实测
           provider=bedrock, model=claude-opus-5-5, 回复正常）。

           结论: project_llm 查得不全，不能作为限制依据。
           模型能否用要**实测**，不能靠 project_llm 判断。
        """
        if self._catalog is not None:
            return self._catalog
        cat: dict = {}
        try:
            st, b = self.gql(config.HGE,
                             "query { llm_config(order_by:{display_label:asc}) "
                             "{ id display_label deleted_at } }")
            for m in ((b or {}).get("data") or {}).get("llm_config") or []:
                if m.get("deleted_at"):
                    continue
                lab = (m.get("display_label") or "").lower()
                if lab:
                    cat[lab] = m["id"]
        except Exception:
            pass
        # 默认模型：优先项目默认，其次挂载的第一个，最后全局第一个
        try:
            for m in self.mounted_models():
                if m.get("default"):
                    cat["__default__"] = m["id"]
                    break
            if "__default__" not in cat:
                mm = self.mounted_models()
                if mm:
                    cat["__default__"] = mm[0]["id"]
        except Exception:
            pass
        if "__default__" not in cat and cat:
            cat["__default__"] = next(iter(cat.values()))
        self._catalog = cat
        return cat

    def global_models(self) -> list[dict]:
        """全局 llm_config（可能未挂载到本项目，仅供展示/排查）。"""
        try:
            st, b = self.gql(config.HGE,
                             "query { llm_config(order_by:{display_label:asc}) "
                             "{ id display_label deleted_at } }")
            return [m for m in (((b or {}).get("data") or {}).get("llm_config") or [])
                    if not m.get("deleted_at")]
        except Exception:
            return []

    def resolve_model(self, name: str | None) -> str:
        """
        把模型名解析成**本项目已挂载**的 llmConfigId。

        🔴 关键: 只从挂载列表里选。给未挂载的 id 不会报错，
           而是让 agent 静默返回 `<done />`，很难排查。
        """
        def norm(x: str) -> str:
            return re.sub(r"[^a-z0-9]", "", (x or "").lower())

        cat = self.model_catalog()
        mounted_ids = {m["id"] for m in self.mounted_models()}
        want = norm(name)

        def pick(k: str) -> str | None:
            # 🔴 不再强制要求命中挂载列表 —— 实测 project_llm 查不全：
            #    Playground 的 project_llm 只返回 GPT-6.1 Sol，
            #    但直接传 Claude Opus 5.5 的 id 完全可用（provider=bedrock）。
            #    所以以「能调通」为准，挂载列表仅作展示。
            return cat.get(k)

        if want:
            for k in cat:
                if k not in ("__default__", "__fallback__") and norm(k) == want:
                    v = pick(k)
                    if v:
                        return v
            for k in cat:
                if k in ("__default__", "__fallback__"):
                    continue
                nk = norm(k)
                if want in nk or nk in want:
                    v = pick(k)
                    if v:
                        return v
        if cat.get("__default__"):
            return cat["__default__"]
        # 兜底：挂载列表里的第一个
        mm = self.mounted_models()
        if mm:
            return mm[0]["id"]
        return ""

    def threads(self, limit: int = 20) -> list[dict]:
        st, b = self.gql(config.HGE, """query T($l:Int!) {
              threads_v2(order_by:{created_at:desc}, limit:$l) {
                thread_id title created_at visibility } }""", {"l": limit})
        return ((b or {}).get("data") or {}).get("threads_v2") or []


CREATE_PROJECT = """mutation ddnCreatePromptQLProject($name: String, $title: String,
  $is_joinable: Boolean) {
  ddnCreatePromptQLProject(name: $name, title: $title, is_joinable: $is_joinable) { id name }
}"""

LIST_MY_PROJECTS = """query MyProjects {
  ddn_projects(order_by: {created_at: desc}) { id name title plan_name }
}"""


def ensure_project(sess: "Session") -> str:
    """
    确保该账号有一个自己的项目，返回 project_id。
    新注册账号 user-accesses 为空，必须先建项目才能用 PromptQL。

    优先复用已存在的（避免重复建），本地 project_id 有效时直接返回。
    """
    if sess.project_id:
        return sess.project_id
    st, b = sess.gql(config.CONTROL_PLANE, LIST_MY_PROJECTS, None,
                     bearer=None, cookie=True)
    rows = ((b or {}).get("data") or {}).get("ddn_projects") or []
    if rows:
        sess.project_id = rows[0]["id"]
        return sess.project_id

    import uuid
    name = "p-" + uuid.uuid4().hex[:10]
    st2, b2 = sess.gql(config.CONTROL_PLANE, CREATE_PROJECT,
                       {"name": name, "title": "Gateway", "is_joinable": False},
                       bearer=None, cookie=True)
    pid = (((b2 or {}).get("data") or {}).get("ddnCreatePromptQLProject") or {}).get("id")
    if not pid:
        raise UpstreamError(f"建项目失败: {json.dumps(b2, ensure_ascii=False)[:300]}")
    sess.project_id = pid
    return pid


OLU_RATE_Q = """query FetchProjectUsagePlanMode($projectId: uuid!) {
  project_configuration_by_pk(project_id: $projectId) { usage_plan_mode }
  project_billing_olu_config_versions(where: {project_id: {_eq: $projectId}},
    order_by: {version: desc}, limit: 1) { olu_base_price_usd_micros }
}"""


def olu_info(sess: "Session") -> dict:
    """
    返回免费额度三件套（实测口径）:
      plan    : OLU_BASED | USD_BASED  ← 决定账号到底有没有额度
      rate    : 每 OLU 的美元价（实测 0.14）
      granted / used / remaining / remaining_usd
    """
    out = {"plan": "", "rate": None, "granted": None, "used": None,
           "remaining": None, "remaining_usd": None}
    try:
        st, b = sess.gql(config.HGE, OLU_RATE_Q, {"projectId": sess.project})
        d = (b or {}).get("data") or {}
        out["plan"] = ((d.get("project_configuration_by_pk") or {}).get("usage_plan_mode") or "")
        rows = d.get("project_billing_olu_config_versions") or []
        if rows:
            m = rows[0].get("olu_base_price_usd_micros")
            if m:
                out["rate"] = float(m) / 1e6
    except Exception:
        pass
    try:
        olu = sess.olu()
        g, u = olu.get("granted"), olu.get("used")
        out["granted"], out["used"] = g, u
        if g is not None:
            rem = max(float(g) - float(u or 0), 0)
            out["remaining"] = rem
            if out["rate"]:
                out["remaining_usd"] = rem * out["rate"]
    except Exception:
        pass
    return out


def _tripwire(body) -> None:
    """
    检测银行卡/付费墙信号 —— 命中立刻熔断。

    🔴 只看 **错误消息**，不看整个响应体。
       踩过的坑: 早先版本检查整个 JSON，结果把**我自己查询里的字段名**
       （例如 `query { user_billing { ... } }`）当成了付费信号，
       直接误熔断整个流程。

    另外只认「明确的支付要求」，不认泛泛的 billing 字样。
    """
    if isinstance(body, str):
        return
    msgs = []
    for e in (body or {}).get("errors") or []:
        m = e.get("message") if isinstance(e, dict) else str(e)
        if m:
            msgs.append(m)
    text = " | ".join(msgs).lower()
    if not text:
        return

    STRICT = (
        "payment required", "requires a card", "add a credit card",
        "add a payment method", "upgrade your plan", "insufficient credit",
        "billing required", "card required", "requires payment",
        "需要绑卡", "需要付款", "余额不足",
    )
    for kw in STRICT:
        if kw in text:
            raise PaymentRequired(f"上游明确要求付费/绑卡: {kw!r}（原文 {text[:180]}）")


# ---- 事件流解析（🔴 关键：真正的答案在 action_completed 里，不在 llm_response）----

def extract_final(events: list) -> dict:
    """
    从 thread_events 里抽三样东西：

      final        —— 真正面向用户的最终答复
                      （action_completed.result.message，类型 final_response_sent）
      thinking     —— 最后一次 llm_response.thinking_text
      tool_trace   —— 工具执行轨迹（run_shell / write_file / run_program …）

    🔴 为什么必须这么取（踩坑记录）:
       llm_response.response_text 里装的是 agent 的**中间动作 XML**
       （<action><run_shell>…</run_shell></action>），
       真正的回答在后面的 action_completed.result.message 里。
       只读 response_text 会拿到空内容 —— 表现为「跑了几分钟，返回 <done />」，
       从而误判成「模型不行 / 只会查 wiki」。
    """
    final = ""
    thinking = ""
    tools: list[dict] = []
    usage: dict = {}
    n_fail = 0

    for ev in events or []:
        ed = ev.get("event_data") or {}
        am = ed.get("AgentMessage") or {}
        upd = ((am.get("update") or {}).get("content") or {}).get("interaction_update") or {}
        ma = upd.get("main_agent") or {}
        if not ma:
            continue

        lr = ma.get("llm_response") or {}
        if lr:
            if lr.get("thinking_text"):
                thinking = lr["thinking_text"]
            if lr.get("usage"):
                usage = lr["usage"]
            # post_in_chat 形式的直接回答
            rt = lr.get("response_text") or ""
            if is_user_facing(rt):
                t = extract_text(rt)
                if t:
                    final = t

        # 工具调用
        for key in ("actions_parsed",):
            for a in ((ma.get(key) or {}).get("actions") or []):
                if isinstance(a, dict):
                    for name, arg in a.items():
                        tools.append({"name": name, "arg": arg})

        ac = (ma.get("action_completed") or {}).get("result") or {}
        rtype = ac.get("agent_loop_action_result_type")
        if rtype == "final_response_sent":
            msg = ac.get("message") or ""
            if msg:
                final = msg                    # ← 覆盖：这才是最终答复
        elif rtype == "shell_executed":
            if ac.get("exit_code") not in (0, None):
                n_fail += 1
            tools.append({"name": "shell_result",
                          "arg": {"cmd_out": (ac.get("output") or "")[:400],
                                  "stderr": (ac.get("stderr") or "")[:200],
                                  "exit_code": ac.get("exit_code")}})

    return {"final": final, "thinking": thinking, "tools": tools,
            "usage": usage, "tool_failures": n_fail}


# ---- 加入公开项目（🔴 新账号拿免费额度的关键一步）----

PUBLIC_PROJECTS_Q = """query { promptql_public_project { project_id title description } }"""

JOIN_PUBLIC = """mutation JP($project_id: uuid!) {
  ddnJoinPublicProject(project_id: $project_id) { __typename }
}"""


def public_projects(sess: "Session") -> list[dict]:
    """平台公开项目列表（实测含 Playground）。"""
    st, b = sess.gql(config.CONTROL_PLANE, PUBLIC_PROJECTS_Q, None,
                     bearer=None, cookie=True)
    return (((b or {}).get("data") or {}).get("promptql_public_project")) or []


def join_public_project(sess: "Session", project_id: str) -> bool:
    """
    自助加入公开项目。

    🔴 实测关键: 新注册账号默认是 USD_BASED、$0 额度。
       但 `ddnJoinPublicProject` 是**任何人可自助调用**的 ——
       加入 Playground（promptql-community，OLU_BASED）后立刻获得
       granted_olus=1071.43（≈$150），且能直接用 Opus 5.5。

       注意接口名有 `ddn` 前缀；`JoinPublicProject` 会报 field not found。
    """
    st, b = sess.gql(config.CONTROL_PLANE, JOIN_PUBLIC, {"project_id": project_id},
                     bearer=None, cookie=True)
    if "errors" in (b or {}):
        raise UpstreamError(f"加入公开项目失败: {str(b)[:250]}")
    return bool(((b.get("data") or {}).get("ddnJoinPublicProject")))


def join_all_public(sess: "Session") -> list[dict]:
    """加入所有公开项目，返回结果。"""
    out = []
    for p in public_projects(sess):
        pid = p.get("project_id")
        try:
            ok = join_public_project(sess, pid)
            out.append({"project_id": pid, "title": p.get("title"), "ok": ok})
        except Exception as e:
            out.append({"project_id": pid, "title": p.get("title"),
                        "ok": False, "error": f"{type(e).__name__}: {e}"})
    return out


# ---- 一次完整调用（同步，内部轮询事件流） ----

START_THREAD = """mutation StartThreadRoomlessWithModel($message: String!, $projectId: String!,
  $timezone: String!, $llmConfigId: String!, $agentResponseConfig: String) {
  start_thread(message: $message, projectId: $projectId, timezone: $timezone,
    llmConfigId: $llmConfigId, roomless: true, agentResponseConfig: $agentResponseConfig) {
    thread_id title created_at
  }
}"""

EVENTS = """query getThreadEvents($thread_id: uuid!, $after_event_id: bigint!) {
  thread_events(where: {thread_id: {_eq: $thread_id}, thread_event_id: {_gt: $after_event_id}},
                order_by: {thread_event_id: asc}) {
    thread_event_id event_data created_at
  }
}"""


@dataclass
class CallResult:
    text: str
    thread_id: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    latency_ms: int
    ttft_ms: int = 0            # 首字延迟：从发起到第一次出现文本
    thinking: str = ""          # agent 的 thinking_text（推理过程）
    thinking_ms: int = 0        # 思考耗时
    tools: list = field(default_factory=list)   # 工具执行轨迹
    tool_failures: int = 0
    raw_events: list = field(default_factory=list)


# agent 动作里只有这些是「面向用户的回复」；其余（learning_block / post_to_parent
# / 各种内部 action）对下游调用方没有意义，不能当成回复返回。
_USER_FACING = ("post_in_chat", "reply")


def is_user_facing(raw: str) -> bool:
    """这段 response_text 里有没有真正给用户看的回复。"""
    if not raw:
        return False
    import re
    return any(re.search(rf"<{t}[^>]*>", raw) for t in _USER_FACING)


def extract_text(raw: str) -> str:
    """把 <action><post_in_chat>…</post_in_chat></action> 拆成纯文本。"""
    if not raw:
        return ""
    import re
    for t in _USER_FACING:
        m = re.findall(rf"<{t}[^>]*>(.*?)</{t}>", raw, re.S)
        if m:
            got = "\n".join(x.strip() for x in m).strip()
            if got:
                return got
    # 没有用户可见回复：只清掉动作标签，别把内部内容当回复
    t = re.sub(r"</?(action|learning_block|post_to_parent|thinking)[^>]*>", " ", raw)
    return re.sub(r"\s+", " ", t).strip()


def call(sess: Session, prompt: str, *, model: str | None = None,
         timezone_name: str = "America/Los_Angeles",
         wait_s: int | None = None, poll_s: float | None = None,
         on_delta=None) -> CallResult:
    """
    发一条消息并等完整回复。on_delta(text) 可选，用于流式回调。
    """
    sess.ensure()
    # 🔴 模型按项目解析 —— 直接套全局 id 会 start_thread internal error
    mid = sess.resolve_model(model)
    t0 = time.time()

    st, b = sess.gql(config.HGE, START_THREAD, {
        "message": prompt,
        "projectId": sess.project,
        "timezone": timezone_name,
        "llmConfigId": mid,
        "agentResponseConfig": "force_respond",
    })
    if st != 200 or "errors" in (b or {}):
        err = json.dumps(b, ensure_ascii=False)[:400]
        if "llm" in err.lower() and "config" in err.lower():
            raise UpstreamError(f"模型不可用: {err}")
        raise UpstreamError(f"start_thread 失败: {err}")
    th = b["data"]["start_thread"]["thread_id"]

    after = 0
    deadline = time.time() + (wait_s or config.LLM_WAIT_S)
    usage, reply, thinking = None, None, None
    seen: list = []
    emitted = 0
    ttft_ms = 0
    t_think_end = 0

    while time.time() < deadline:
        st2, b2 = sess.gql(config.HGE, EVENTS, {"thread_id": th, "after_event_id": after})
        if st2 == 200 and "data" in (b2 or {}):
            for ev in b2["data"]["thread_events"]:
                after = max(after, int(ev["thread_event_id"]))
                seen.append(ev)
                am = (ev.get("event_data") or {}).get("AgentMessage") or {}
                upd = ((am.get("update") or {}).get("content") or {}) \
                    .get("interaction_update") or {}
                ma = upd.get("main_agent") or {}
                lr = ma.get("llm_response") or {}
                txt = lr.get("response_text")
                if txt:
                    # 首字延迟 = 第一次拿到面向用户的文本的时刻
                    if not ttft_ms and is_user_facing(txt):
                        ttft_ms = int((time.time() - t0) * 1000)
                    reply = txt
                    if on_delta:
                        plain = extract_text(txt)
                        if len(plain) > emitted:
                            on_delta(plain[emitted:])
                            emitted = len(plain)
                if lr.get("thinking_text"):
                    thinking = lr["thinking_text"]
                    if not t_think_end and txt:
                        t_think_end = time.time()
                if lr.get("usage"):
                    usage = lr["usage"]
            # 🔴 agent 可能先发 learning_block（无用户可见内容）再发正式回复。
            #    必须等到真正面向用户的那条，否则下游会收到空/内部内容。
            if usage and reply and is_user_facing(reply):
                break
        time.sleep(poll_s or config.LLM_POLL_S)

    parsed = extract_final(seen)
    final = parsed["final"] or extract_text(reply) if reply else parsed["final"]
    if not final and not parsed["tools"]:
        raise UpstreamError(
            f"等待超时（{wait_s or config.LLM_WAIT_S}s），thread={th}")
    if not final:
        # 有工具轨迹但没最终答复 —— 把轨迹摘要作为结果，别丢
        lines = ["[agent 未给出文字答复，但执行了 %d 个工具动作]" % len(parsed["tools"])]
        for t in parsed["tools"][-6:]:
            lines.append("- %s: %s" % (t["name"], str(t.get("arg"))[:200]))
        final = "\n".join(lines)
    u = parsed["usage"] or usage or {}
    return CallResult(
        text=final,
        thread_id=th,
        model=str(u.get("model") or ""),
        provider=str(u.get("provider") or ""),
        input_tokens=int(u.get("input_tokens") or 0),
        output_tokens=int(u.get("output_tokens") or 0),
        cached_tokens=int(u.get("cached_tokens") or 0),
        latency_ms=int((time.time() - t0) * 1000),
        ttft_ms=ttft_ms or int((time.time() - t0) * 1000),
        thinking=parsed["thinking"] or (thinking or ""),
        thinking_ms=int((t_think_end - t0) * 1000) if t_think_end else 0,
        tools=parsed["tools"],
        tool_failures=parsed["tool_failures"],
        raw_events=seen,
    )
