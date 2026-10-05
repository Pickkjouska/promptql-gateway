# -*- coding: utf-8 -*-
"""
register.py —— 注册编排

流程（实测得出）:
  1. 邮箱 OTP 登录（首次登录即建号）
     ⚠️ /otp/send 硬性要求 reCAPTCHA v3 token → 必须借浏览器上下文取
        {'error': 'Captcha verification is required'}  ← 不带 token 的直接结果
  2. 登录后查 /phone/onboarding-status
  3. 若要求手机验证 → 5sim 接码（硬闸 <= $0.10）→ 提交
  4. 落盘 hasura-lux cookie（后续免浏览器）

纪律:
  - 命中银行卡/付费信号 → PaymentRequired，立即中止，不重试
  - 5sim 每次注册最多下单 1 次（用户要求「先做一次的测试」）
  - 一账号一浏览器 profile（指纹隔离）
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .. import config
from .http import http
from . import fivesim, hero_sms, outlook, store
from .promptql import PaymentRequired


def _sms_module():
    """按配置选接码提供商。"""
    return hero_sms if config.sms_provider() == "hero" else fivesim


def _fivesim_buy_rotation(*, submit, verify, on_event):
    """把统一调用转成具体提供商的换号闭环。"""
    if config.sms_provider() == "hero":
        return hero_sms.buy_code_with_rotation(
            submit=submit, verify=verify,
            country=config.HERO_SMS_COUNTRY,
            service=config.HERO_SMS_SERVICE,
            operator=(config.HERO_SMS_OPERATOR or None),
            settle_before_retry=config.HERO_SETTLE_BEFORE_RETRY,
            on_event=on_event)
    return fivesim.buy_code_with_rotation(
        submit=submit, verify=verify,
        country=config.FIVESIM_COUNTRY, product=config.FIVESIM_PRODUCT,
        on_event=on_event)


class RegisterError(RuntimeError):
    pass


class PhoneRequired(RegisterError):
    """需要手机验证但拿不到号码/超价。"""


@dataclass
class RegResult:
    email: str
    ok: bool
    stage: str
    cookie: str = ""
    phone: str = ""
    phone_verified: bool = False
    olu: dict = field(default_factory=dict)
    error: str = ""
    meta: dict = field(default_factory=dict)


# ---------------- 浏览器侧：取 captcha token + 完成 OTP ----------------

def _browser_login(email: str, profile_dir: Path, code_getter,
                   headless: bool = True, timeout_s: int = 120,
                   phone_hook=None, on_step=None) -> dict:
    """
    用 Camoufox 打开登录页，驱动 email → OTP 两步。
    code_getter() -> str  由调用方提供（读邮箱）。
    phone_hook(page) -> dict  可选。**新账号 OTP 通过后拿到的不是 hasura-lux，
        而是 hasura-phone-onboarding** —— 手机验证必须在这个 onboarding 会话里完成，
        完成后服务端才会补发 hasura-lux。所以钩子要在浏览器上下文里跑。

    返回 {'cookie': ..., 'cookies': {...}, 'onboarding_cookie': ...,
          'final_url': ..., 'recaptcha_ready': bool}
    """
    import sys
    # 复用项目既有的指纹身份模块（四要素可复现）。
    # 独立部署（Linux 包）里可能没有 registrars/_shared —— 降级为 Camoufox 默认指纹。
    F = None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
        from _shared import fp_identity as F
    except Exception:
        F = None

    if F is not None:
        prof = str(profile_dir)
        ident = F.load(prof) if F.exists(prof) else F.assign(prof, site="promptql-community")
        launcher = lambda: F.launch(ident, headless=headless)
    else:
        from camoufox.sync_api import Camoufox
        # 降级：仍保证「一账号一进程」，但指纹不可跨进程复现
        launcher = lambda: Camoufox(headless=headless, humanize=True,
                                    os="windows", main_world_eval=True)

    def step(name: str, **extra):
        if on_step:
            on_step(name, **extra)

    result: dict = {}
    step("启动浏览器进程")
    with launcher() as browser:
        step("浏览器已就绪")
        ctx = browser.new_context() if hasattr(browser, "new_context") else browser
        page = ctx.new_page()
        page.set_default_timeout(20_000)   # 降下来：locator 的 auto-wait 最坏就是这个数
        step("打开登录页")
        page.goto(f"{config.SITE}/login", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3000)

        # 等 reCAPTCHA 就绪
        ready = False
        for _ in range(25):
            ready = bool(page.evaluate(
                "mw:" + "() => !!(window.grecaptcha && window.grecaptcha.execute)"))
            if ready:
                break
            page.wait_for_timeout(700)
        result["recaptcha_ready"] = ready
        step("reCAPTCHA 就绪" if ready else "reCAPTCHA 未就绪(仍继续)", ready=ready)

        inp = page.locator('input[data-testid*="otp-email-input"], input[type="email"], input').first
        inp.click()
        inp.fill("")
        inp.type(email, delay=45)
        page.wait_for_timeout(500)

        btn = page.locator('[data-testid*="otp-continue"]').first
        if btn.count() == 0:
            import re
            btn = page.get_by_role("button", name=re.compile(r"continue", re.I)).first
        step("填写邮箱，点击 Continue（将触发发信）", email=email)
        btn.click()
        page.wait_for_timeout(9000)
        body = page.locator("body").inner_text() or ""
        result["after_send_text"] = body[:400]
        step("已进入验证码页，等待收码", ui=body[:120])

        # 取验证码（读邮箱）
        code = code_getter()
        result["code_used"] = code
        step("取到验证码", code=code)

        # ---- 填验证码 ----
        # 实测: 页面是**单个** input[aria-label="Verification code"]，
        #       不是 Mantine 六格 PinInput（早期判断有误，截图已证）。
        #
        # 🔴 间歇性失败（实测抓到）: click 没真正聚焦 → keyboard.type 打到虚空
        #    → 输入框为空 → Continue 永不启用 → 若用 is_enabled() 死等会挂 18 分钟。
        #    所以这里必须「填完就校验，不合格换策略重试」，绝不能盲信一次性动作。
        def read_code_input() -> str:
            return page.evaluate("mw:" + """() => {
                const i = document.querySelector('input[aria-label="Verification code"]')
                       || document.querySelector('input[data-testid*="otp-code-input"]');
                return i ? i.value : '';
            }""")

        def is_focused() -> bool:
            return page.evaluate("mw:" + """() => {
                const a = document.activeElement;
                return !!(a && a.tagName === 'INPUT');
            }""")

        strategies = [
            ("click+type", lambda: (
                page.locator('input[aria-label="Verification code"]').first.click(timeout=6000),
                page.wait_for_timeout(250),
                page.keyboard.type(code, delay=120))),
            ("fill()", lambda: (
                page.locator('input[aria-label="Verification code"]').first.fill(code, timeout=6000))),
            ("focus+type", lambda: (
                page.evaluate("mw:" + """() => {
                    const i = document.querySelector('input[aria-label="Verification code"]')
                           || document.querySelector('input[data-testid*="otp-code-input"]')
                           || document.querySelector('input');
                    if (i) { i.focus(); i.select && i.select(); }
                }"""),
                page.wait_for_timeout(200),
                page.keyboard.type(code, delay=100))),
            ("每格 fill", lambda: [page.locator('input[aria-label="Verification code"]')
                                     .nth(k).fill(code[k]) for k in range(min(6, len(code)))]),
        ]

        filled = ""
        used = ""
        for name, action in strategies:
            try:
                action()
                page.wait_for_timeout(600)
                filled = read_code_input()
                used = name
                if filled.replace(" ", "") == code:
                    break
            except Exception as e:
                step(f"填码策略 {name} 异常", err=f"{type(e).__name__}: {str(e)[:80]}")
                continue

        step("填码完成", 策略=used, 实际值=filled, 期望=code, 聚焦=is_focused())

        if filled.replace(" ", "") != code:
            try:
                page.screenshot(path=str(config.DATA / "fail_otp_fill.png"))
            except Exception:
                pass
            raise RegisterError(
                f"验证码未能填入输入框：4 种策略试过，最后值={filled!r}，期望={code!r}"
                f"（页面可能改版或元素被遮挡；已存截图 {config.DATA}/fail_otp_fill.png）")

        # 🔴🔴 血的教训（实测）:
        #   Playwright 的 locator.is_enabled() 不是即时查询，而是 **auto-wait 式**查询 ——
        #   元素不存在时会一直等到 page default timeout（我们设了 45s）。
        #   我原来写了 24 次循环 → 最坏卡 18 分钟，表现为「注册卡死且无日志」。
        #   所以这里一律用 page.evaluate 直读 DOM（零 auto-wait），并给等待设硬上界。
        def btn_state() -> dict:
            return page.evaluate("mw:" + """() => {
                const b = document.querySelector('[data-testid*="otp-verify"]')
                       || Array.from(document.querySelectorAll('button'))
                            .find(x => /continue|verify|sign in/i.test(x.innerText||''));
                if (!b) return {found:false};
                return {found:true, disabled:!!b.disabled, loading:!!b.getAttribute('data-loading'),
                        text:(b.innerText||'').trim().slice(0,40)};
            }""")

        def pin_value() -> str:
            return page.evaluate("mw:" + """() => {
                const i = document.querySelector('input[aria-label="Verification code"]');
                return i ? i.value : '';
            }""")

        st0 = btn_state()
        step("填入后按钮状态", **st0)

        # 最多等 10 秒让它变可用（正常情况下本来就是可用的）
        waited = 0
        while st0.get("disabled") and waited < 10:
            page.wait_for_timeout(1000)
            waited += 1
            st0 = btn_state()

        if st0.get("disabled"):
            raise RegisterError(
                f"验证码未被接受：输入框={pin_value()!r}，期望={code!r}，"
                f"按钮仍 disabled（码过期或被服务端拒绝）")

        # 点提交。页面跳转会中断 click —— 那是成功信号，不当失败。
        step("点击 Continue 提交")
        click_err = ""
        try:
            page.locator('[data-testid*="otp-verify"]').first.click(timeout=8000)
        except Exception as e:
            click_err = f"{type(e).__name__}: {str(e)[:100]}"
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass
        if click_err:
            step("click 被中断（多为页面已跳转）", err=click_err)

        # 轮询 URL 变化（有界，最多 25s）
        for _ in range(25):
            page.wait_for_timeout(1000)
            if "/login" not in page.url:
                break
        step("OTP 提交完成", url=page.url)

        step("OTP 提交完成", url=page.url)

        result["final_url"] = page.url
        result["body_after"] = (page.locator("body").inner_text() or "")[:800]

        def _jar() -> dict:
            return {c.get("name"): (c.get("value") or "") for c in ctx.cookies()}

        jar = _jar()
        result["cookies_all"] = list(jar.keys())
        result["cookies"] = jar

        # 新账号第一次登录：拿到的是 hasura-phone-onboarding，还没有 hasura-lux。
        # 手机验证必须在这个 onboarding 会话里完成，服务端才会补发 hasura-lux。
        step("检查 cookie", got_lux=bool(jar.get("hasura-lux")),
             jar=list(jar.keys()))
        if not jar.get("hasura-lux") and phone_hook is not None:
            step("需要手机验证（onboarding），开始接码")
            result["phone_onboarding_cookie"] = jar.get("hasura-phone-onboarding", "")
            try:
                result["phone"] = phone_hook(page, jar.get("hasura-phone-onboarding", ""))
            except Exception as e:
                result["phone_error"] = f"{type(e).__name__}: {e}"
                step("手机验证失败", err=f"{type(e).__name__}: {e}"[:160])
                store.log("warn", "phone", f"onboarding 手机验证失败: {e}", email)
            else:
                step("手机验证完成", phone=result.get("phone", {}).get("phone"))
            # 验证后再抓一次 cookie
            page.wait_for_timeout(2000)
            jar = _jar()
            result["cookies_all"] = list(jar.keys())
            result["cookies"] = jar

        result["cookie"] = jar.get("hasura-lux", "")
        try:
            ctx.close() if hasattr(browser, "new_context") else None
        except Exception:
            pass
    return result


# ---------------- 手机验证 ----------------

def _phone_status(sess) -> dict:
    r = http.get(f"{config.AUTH_HOST}/phone/onboarding-status",
                     headers={"cookie": f"hasura-lux={sess.cookie}"},
                     proxies=config.proxy_dict(), timeout=config.HTTP_TIMEOUT)
    if r.status_code == 401:
        return {"required": False, "raw": r.text[:120]}
    try:
        return {"required": True, "raw": r.json()}
    except Exception:
        return {"required": False, "raw": r.text[:120]}


def _phone_flow_in_browser(page, onboarding_cookie: str, email: str) -> dict:
    """
    在浏览器上下文里完成手机 onboarding。
    必须走页面 fetch：该会话由 hasura-phone-onboarding cookie 标识，
    且服务端会校验同源/CORS —— 用 requests 从外部打会缺 cookie。
    收不到码会自动退单换号（上限 config.FIVESIM_MAX_TRIES）。
    """
    def js(path: str, method: str, body=None):
        return page.evaluate("mw:" + """async ([url, method, body]) => {
            const opt = {method, credentials: 'include',
                         headers: body ? {'Content-Type': 'application/json'} : undefined};
            if (body) opt.body = JSON.stringify(body);
            try {
                const r = await fetch(url, opt);
                const t = await r.text();
                return {status: r.status, body: t.slice(0, 800)};
            } catch (e) { return {status: -1, err: String(e)}; }
        }""", [f"{config.AUTH_HOST}{path}", method, body])

    def submit(phone: str):
        r = js("/phone/send-verification", "POST", {"phone_number": phone})
        st = r.get("status", -1)
        # 429/502 这类是上游自身问题，把响应体原样带出去给错误分类器判断
        if st >= 400 or st < 0:
            raise PhoneRequired(
                f"提交号码失败 HTTP {st}: {r.get('body') or r.get('err','')}")

    def verify(code: str) -> bool:
        r = js("/phone/verify-otp", "POST", {"code": code})
        verify.last = r
        try:
            import json as _j
            return _j.loads(r.get("body") or "{}").get("status") == "verified"
        except Exception:
            return False

    def on_event(msg: str, data: dict):
        store.log("info", "phone", msg, email, data)

    code, att, history = _fivesim_buy_rotation(
        submit=submit, verify=verify, on_event=on_event)

    return {
        "order": att.order_id, "phone": att.phone, "verified": True, "code": code,
        "resp": getattr(verify, "last", {}),
        "tries": len(history),
        "spent_usd": round(sum(a.price for a in history), 4),
        "history": [{"order": a.order_id, "phone": a.phone, "status": a.status,
                     "reason": a.reason[:120]} for a in history],
    }


def _phone_flow(sess, email: str, *, allow_buy: bool = True) -> dict:
    """
    已登录会话（有 hasura-lux）下的手机验证。用于存量账号被要求补验证的场景。
    **收不到码会自动退单换新号**（上限见 config.FIVESIM_MAX_TRIES）。
    总花费受 FIVESIM_TOTAL_BUDGET 约束；单号单价超闸直接拒绝。
    """
    if not allow_buy:
        raise PhoneRequired("未允许自动接码")

    def submit(phone: str):
        r = http.post(f"{config.AUTH_HOST}/phone/send-verification",
                          headers={"cookie": f"hasura-lux={sess.cookie}",
                                   "content-type": "application/json"},
                          json={"phone_number": phone},
                          proxies=config.proxy_dict(), timeout=config.HTTP_TIMEOUT)
        if r.status_code >= 400:
            raise PhoneRequired(f"提交号码失败 HTTP {r.status_code}: {r.text[:200]}")

    def verify(code: str) -> bool:
        r2 = http.post(f"{config.AUTH_HOST}/phone/verify-otp",
                           headers={"cookie": f"hasura-lux={sess.cookie}",
                                    "content-type": "application/json"},
                           json={"code": code},
                           proxies=config.proxy_dict(), timeout=config.HTTP_TIMEOUT)
        try:
            jr = r2.json()
        except Exception:
            jr = {"raw": r2.text[:200]}
        verify.last = jr
        return jr.get("status") == "verified"

    def on_event(msg: str, data: dict):
        store.log("info", "phone", msg, email, data)

    code, att, history = _fivesim_buy_rotation(
        submit=submit, verify=verify, on_event=on_event)

    return {
        "order": att.order_id,
        "phone": att.phone,
        "verified": True,
        "code": code,
        "resp": getattr(verify, "last", {}),
        "tries": len(history),
        "spent_usd": round(sum(a.price for a in history), 4),
        "history": [{"order": a.order_id, "phone": a.phone, "status": a.status,
                     "reason": a.reason[:120]} for a in history],
    }


# ---------------- 主入口 ----------------

def register(email: str, *, cred_raw: str = "", password: str = "",
             profile_dir: Path | None = None, headless: bool = True,
             allow_phone: bool = True, timeout_otp: int = 180) -> RegResult:
    """
    一个账号的完整注册。
    cred_raw: 邮箱凭据（四段式）。为空则从 DB 读。
    """
    # ---- 邮箱通道：**只走 outlook（RT 凭据）** ----
    # 🔴 不支持临时邮箱：共享域名容易被上游风控识别，且不可长期持有。
    #    必须提供四段式凭据 email----password----refresh_token----client_id，
    #    或该账号已在库中存过 refresh_token。
    mail_cred = None

    if cred_raw:
        mail_cred = outlook.parse(cred_raw)
        email = mail_cred.email.strip().lower()
        store.set_secret(email, rt=mail_cred.refresh_token,
                         client_id=mail_cred.client_id,
                         password=mail_cred.password or None)
    else:
        s = store.get_secret(email) if email else {}
        if s.get("rt"):
            mail_cred = outlook.MailCred(email=email, refresh_token=s["rt"],
                                         client_id=s["client_id"],
                                         password=s.get("password") or "")

    t_before = time.time()

    if mail_cred is None:
        return RegResult(email or "?", False, "mail_cred",
                         error="必须提供邮箱凭据（格式 email----password----"
                               "refresh_token----client_id）。本网关不支持临时邮箱。")

    if mail_cred is not None:
        store.set_secret(email, rt=mail_cred.refresh_token, client_id=mail_cred.client_id)

        def get_code_outlook() -> str:
            code, meta = outlook.fetch_otp(mail_cred, since_ts=t_before,
                                           timeout_s=timeout_otp)
            store.log("info", "register",
                      f"邮箱取到验证码 {code}（{meta.get('subject','')[:60]}）", email)
            return code

        code_getter = get_code_outlook
    else:
        return RegResult(email or "?", False, "mail_cred", error="没有可用的邮箱凭据")

    email = email.strip().lower()
    aid = store.upsert_account(email, status="registering")

    prof = profile_dir or (config.DATA / "profiles" / email.replace("@", "_at_"))

    def phone_hook(page, onboarding_cookie: str) -> dict:
        """在浏览器里完成手机验证（新账号首次登录的必经环节）。
        做完服务端才补发 hasura-lux。"""
        if not allow_phone:
            raise PhoneRequired("未允许自动接码，但该账号需要手机验证")
        return _phone_flow_in_browser(page, onboarding_cookie, email)

    # 全程埋点: 每一步都落库, 前端「日志」页可实时看到卡在哪一步
    _t0 = time.time()

    def step(name: str, **extra):
        el = time.time() - _t0
        store.log("info", "step", f"[{el:6.1f}s] {name}", email,
                  extra if extra else None)

    try:
        step("开始：准备启动指纹浏览器", profile=str(prof), headless=headless)
        res = _browser_login(email, prof, code_getter, headless=headless,
                             phone_hook=phone_hook, on_step=step)
    except PaymentRequired as e:
        store.upsert_account(email, status="blocked_card", note=str(e)[:300])
        store.log("error", "register", f"银行卡熔断: {e}", email)
        return RegResult(email, False, "login", error=str(e))
    except Exception as e:
        store.upsert_account(email, status="failed", note=str(e)[:300])
        store.log("error", "register", f"登录失败: {e}", email)
        return RegResult(email, False, "login", error=f"{type(e).__name__}: {e}")

    cookie = res.get("cookie") or ""
    if not cookie:
        store.upsert_account(email, status="failed",
                             note=f"没拿到 hasura-lux; cookies={res.get('cookies_all')}")
        store.log("error", "register",
                  f"没拿到 hasura-lux cookie（页面={res.get('final_url')}）", email, res)
        return RegResult(email, False, "cookie", error=f"cookies={res.get('cookies_all')}")

    store.set_secret(email, cookie=cookie)
    store.upsert_account(email, status="logged_in")

    # 建立会话，做身份/额度探测
    from .promptql import Session
    sess = Session(cookie=cookie)
    meta = {"final_url": res.get("final_url")}
    try:
        sess.ensure()
        who = sess.whoami()
        # 🔴 第一步：加入平台的公开项目（Playground）。
        #    实测：新账号默认 USD_BASED / $0；加入后立刻拿到
        #    granted_olus≈1071（≈$150）且可用 Opus 5.5。
        #    这比"自建项目"划算得多 —— 自建项目永远 $0。
        joined = []
        try:
            from .promptql import join_all_public, public_projects
            joined = join_all_public(sess)
            meta["public_projects"] = joined
            store.log("info", "register",
                      f"加入公开项目: {[j.get('title') for j in joined if j.get('ok')]}", email)
        except Exception as pe:
            meta["join_err"] = f"{type(pe).__name__}: {pe}"
            store.log("warn", "register", f"加入公开项目告警: {pe}", email)

        # 第二步：仍建一个自己的项目（做 owner 的兜底，以及放自己的东西）
        try:
            from .promptql import ensure_project
            pid = ensure_project(sess)
            meta["own_project"] = pid
        except Exception as pe:
            meta["project_err"] = f"{type(pe).__name__}: {pe}"
            store.log("warn", "register", f"建项目告警: {pe}", email)

        # 第三步：把项目切到**有免费额度的公开项目**。
        # 🔴 必须优先选 Playground（实测只有它是 OLU_BASED 带额度）。
        #    不能盲取 pubs[0] —— 平台多一个公开项目就会选错，账号变成 0 额度。
        try:
            pubs = [j for j in joined if j.get("ok")]
            pick = None
            for j in pubs:                     # 先按标题精确匹配
                if (j.get("title") or "").lower() == "playground":
                    pick = j
                    break
            if pick is None and pubs:          # 退而求其次：名字含 playground / community
                for j in pubs:
                    t = (j.get("title") or "").lower()
                    if "playground" in t or "community" in t:
                        pick = j
                        break
            if pick is None and pubs:
                pick = pubs[0]
            if pick:
                sess.project_id = pick["project_id"]
                sess._catalog = None           # 换项目要重取模型目录
                meta["using_project"] = pick
                store.log("info", "register",
                          f"使用项目: {pick.get('title')} ({pick['project_id'][:12]})",
                          email)
        except Exception as pe:
            store.log("warn", "register", f"切换项目告警: {pe}", email)
        olu = sess.olu()
        meta["whoami"] = who
        meta["olu"] = olu
        meta["project_id"] = sess.project_id
        store.upsert_account(email,
                             pql_user_id=sess.pql_user_id,
                             ctrl_user_id=sess.ctrl_user_id,
                             project_id=sess.project_id,
                             olu_total=olu.get("granted", 0),
                             olu_used=olu.get("used", 0),
                             status="active")
    except PaymentRequired as e:
        store.upsert_account(email, status="blocked_card", note=str(e)[:300])
        store.log("error", "register", f"银行卡熔断(登录后): {e}", email)
        return RegResult(email, False, "post_login", cookie=cookie, error=str(e))
    except Exception as e:
        meta["session_err"] = f"{type(e).__name__}: {e}"
        store.log("warn", "register", f"会话建立告警: {e}", email)

    # 手机验证
    phone_verified = False
    try:
        pst = _phone_status(sess)
        meta["phone_status"] = pst
        if pst.get("required"):
            store.log("info", "phone", "上游要求手机验证，启动接码", email)
            pr = _phone_flow(sess, email, allow_buy=allow_phone)
            phone_verified = bool(pr.get("verified"))
            meta["phone"] = pr
            store.upsert_account(email, phone=pr.get("phone", ""),
                                 status="active" if phone_verified else "needs_phone")
        else:
            phone_verified = True   # 无需手机验证
    except PaymentRequired as e:
        store.upsert_account(email, status="blocked_card", note=str(e)[:300])
        store.log("error", "phone", f"银行卡熔断: {e}", email)
        return RegResult(email, False, "phone", cookie=cookie, error=str(e))
    except Exception as e:
        meta["phone_err"] = f"{type(e).__name__}: {e}"
        store.upsert_account(email, status="needs_phone", note=str(e)[:300])
        store.log("warn", "phone", f"手机验证未完成: {e}", email)

    return RegResult(email=email, ok=True, stage="done", cookie=cookie,
                     phone=meta.get("phone", {}).get("phone", ""),
                     phone_verified=phone_verified,
                     olu=meta.get("olu", {}), meta=meta)
