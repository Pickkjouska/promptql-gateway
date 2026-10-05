# -*- coding: utf-8 -*-
"""
hero_sms.py —— hero-sms.com 接码客户端（SMS-Activate 经典协议）

实测确认:
    base    https://hero-sms.com/stubs/handler_api.php
    getBalance   -> ACCESS_BALANCE:2.376
    getCountries -> {"33":{"eng":"Colombia",...}}          country=33 哥伦比亚
    getPrices?service=ot&country=33 -> {"33":{"ot":{"cost":0.048,"count":1368233}}}
                    ↑ service=ot 就是 "other"（实测 'other' 报 service is incorrect）

下单/取码:
    getNumber / getNumberV2   -> 取号
    getStatus&id=             -> STATUS_WAIT_CODE | STATUS_OK:<code> | STATUS_CANCEL
    setStatus&id=&status=6    -> 完成
    setStatus&id=&status=8    -> 取消（退款）

🔴 价格闸同样生效：单号单价 > config.SMS_MAX_PRICE 直接拒绝。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

import requests

from .. import config
from .http import http


class HeroError(RuntimeError):
    pass


class PriceExceeded(HeroError):
    pass


class HeroNoNumbers(HeroError):
    """这批号收不到码。可换新号。"""


class HeroAbort(HeroError):
    """上游站点侧问题（限流/已绑定/服务故障）—— 换号无用，必须停手。"""


class HeroBudgetExceeded(HeroError):
    pass


B = config.HERO_SMS_BASE


def _key() -> str:
    k = config.hero_key()
    if not k:
        raise HeroError("没有 hero-sms api_key")
    return k


def _api(action: str, **params) -> str:
    p = {"api_key": _key(), "action": action}
    p.update({k: v for k, v in params.items() if v is not None})
    r = http.get(B, params=p, proxies=config.proxy_dict(),
                     timeout=config.HTTP_TIMEOUT)
    if r.status_code != 200:
        raise HeroError(f"hero-sms HTTP {r.status_code}: {r.text[:200]}")
    return r.text.strip()


def _api_json(action: str, **params):
    t = _api(action, **params)
    try:
        import json
        return json.loads(t)
    except Exception:
        raise HeroError(f"hero-sms 返回非 JSON ({action}): {t[:200]}")


def balance() -> float:
    t = _api("getBalance")
    if t.startswith("ACCESS_BALANCE:"):
        return float(t.split(":", 1)[1])
    raise HeroError(f"getBalance 异常: {t[:120]}")


def countries() -> dict:
    return _api_json("getCountries")


def find_country(name: str) -> int | None:
    """按英文名/中文名找国家 id。"""
    n = (name or "").strip().lower()
    for cid, v in (countries() or {}).items():
        if n and (n == (v.get("eng") or "").lower() or n == (v.get("chn") or "").lower()
                  or n in (v.get("eng") or "").lower()):
            return int(v["id"])
    return None


def prices(country: int | str, service: str = "ot") -> dict:
    """返回 {operator: {cost, count, physicalCount}}"""
    c = str(country)
    d = _api_json("getPrices", service=service, country=c)
    return (d or {}).get(c) or {}


def _price_of(country: int | str, service: str, operator: str | None) -> float | None:
    ops = prices(country, service)
    if operator:
        v = ops.get(operator) or {}
        return v.get("cost")
    best = None
    for op, v in (ops or {}).items():
        c = v.get("cost")
        if c is None:
            continue
        if best is None or c < best:
            best = c
    return best


def normalize_phone(raw: str) -> str:
    """
    归一化成 E.164（带 + 前缀）。

    🔴 实测坑: hero-sms 的 getNumber 返回 "573224362126"（国家码+号码，**没有 +**），
       直接提交给 PromptQL 会得到 400 INVALID_PHONE_NUMBER。
    """
    d = re.sub(r"\D", "", str(raw or ""))
    if not d:
        return ""
    return "+" + d

@dataclass
class Order:
    id: str
    phone: str
    price: float
    operator: str = ""
    country: str = ""
    service: str = ""
    raw: dict | None = None


def _parse_number(t: str, price: float) -> Order:
    """解析 getNumber / getNumberV2 的返回。"""
    if t.startswith("ACCESS_NUMBER:"):
        parts = t.split(":")
        if len(parts) < 3:
            raise HeroError(f"ACCESS_NUMBER 格式异常: {t[:120]}")
        return Order(id=parts[1], phone=normalize_phone(parts[2]), price=price)
    try:
        import json
        j = json.loads(t)
        if "activationId" in j or "phoneNumber" in j:
            return Order(id=str(j.get("activationId") or j.get("id")),
                         phone=normalize_phone(j.get("phoneNumber") or j.get("phone")),
                         price=float(j.get("activationCost") or price),
                         raw=j)
    except Exception:
        pass
    raise HeroError(f"取号失败: {t[:200]}")


def buy(country: int | str, service: str = "ot", operator: str | None = None) -> Order:
    """取号。先核价，超闸拒绝。"""
    cap = config.SMS_MAX_PRICE
    price = _price_of(country, service, operator)
    if price is None:
        raise PriceExceeded(f"查不到价格 (country={country} service={service} op={operator})")
    if float(price) > cap + 1e-9:
        raise PriceExceeded(f"单价 ${price} 超闸 ${cap} —— 拒绝取号")

    # 优先 V2（结构化，带真实扣费），回退 V1
    try:
        t = _api("getNumberV2", service=service, country=str(country),
                 operator=operator)
        if not t.startswith("ACCESS_"):
            raise HeroError(t[:200])
        o = _parse_number(t, float(price))
    except Exception:
        t = _api("getNumber", service=service, country=str(country),
                 operator=operator)
        o = _parse_number(t, float(price))

    o.country, o.service, o.operator = str(country), service, operator or ""
    if o.price > cap + 1e-9:
        raise PriceExceeded(f"实际扣费 ${o.price} 超闸 ${cap}（id={o.id}）")
    return o


def status(order_id: str) -> str:
    return _api("getStatus", id=order_id)


def finish(order_id: str):
    return _api("setStatus", id=order_id, status="6")


class EarlyCancelDenied(HeroError):
    """未满最小激活期（实测 120s），暂时退不了 —— 等一会再来。"""

    def __init__(self, msg: str, wait_s: int = 0):
        super().__init__(msg)
        self.wait_s = wait_s


def cancel(order_id: str):
    t = _api("setStatus", id=order_id, status="8")
    if "EARLY_CANCEL_DENIED" in t:
        wait_s = 0
        try:
            import json as _j
            wait_s = int((_j.loads(t).get("info") or {}).get("minActivationTime") or 0)
        except Exception:
            pass
        raise EarlyCancelDenied(f"未满最小激活期（{wait_s}s）：{t[:160]}", wait_s)
    return t


def cancel_when_allowed(order_id: str, *, deadline_s: int = 300,
                        on_event=None) -> bool:
    """
    等满最小激活期再退款（实测 hero-sms 最小激活期 = 120s）。

    到点即退，避免「号没收到码还白扣钱」。
    返回 True=已退，False=超时仍未退成。
    """
    t0 = time.time()
    delay = 5.0
    while time.time() - t0 < deadline_s:
        try:
            cancel(order_id)
            if on_event:
                on_event(f"已退款 order={order_id}", {"order": order_id})
            return True
        except EarlyCancelDenied as e:
            wait = max(e.wait_s - (time.time() - t0), 2)
            if on_event:
                on_event(f"退款未到时间，{int(wait)}s 后重试", {"order": order_id})
            time.sleep(min(wait, 25))
        except Exception as e:
            if on_event:
                on_event(f"退款异常（可能已计费）: {e}", {"order": order_id})
            return False
    return False


def wait_code(order_id: str, timeout_s: int | None = None,
              poll_s: float = 5.0) -> tuple[str, str]:
    """返回 (code, raw_status)。"""
    import re
    deadline = time.time() + (timeout_s or config.SMS_WAIT_S)
    last = ""
    while time.time() < deadline:
        t = status(order_id)
        last = t
        if t.startswith("STATUS_OK:"):
            code = t.split(":", 1)[1].strip()
            m = re.search(r"\d{4,8}", code)
            return (m.group(0) if m else code), t
        if t in ("STATUS_CANCEL",) or "STATUS_CANCEL" in t:
            raise HeroNoNumbers(f"订单被取消（id={order_id}）")
        time.sleep(poll_s)
    raise HeroNoNumbers(f"{timeout_s or config.SMS_WAIT_S}s 内未收到验证码"
                        f"（id={order_id}, 最后状态={last}）")


def buy_code_with_rotation(*, submit, verify,
                           country: int | str | None = None,
                           service: str | None = None,
                           operator: str | None = None,
                           max_tries: int | None = None,
                           total_budget: float | None = None,
                           per_try_wait_s: int | None = None,
                           settle_before_retry: bool = True,
                           on_event=None) -> tuple[str, "A", list]:
    """
    🔴 一账号一码（串行，绝不并发开多个号）。

    流程（每轮只持有一个号）:
        取号 → 提交给目标站 → 等码
          ├─ 收到码 → 提交 → 成功: finish
          │                → 失败: 退单(等满激活期) → 下一轮换新号
          └─ 没收到码/号被拒 → 退单(等满激活期) → 下一轮换新号

    settle_before_retry: 换号前是否等满 hero-sms 最小激活期再退款（默认 True）。
        实测最小激活期 120s，未满会被 EARLY_CANCEL_DENIED 拒绝。
        开着 = 钱能退回来，代价是每轮多等 ~120s。
    """
    c = country if country is not None else config.HERO_SMS_COUNTRY
    svc = service or config.HERO_SMS_SERVICE
    tries = max_tries if max_tries is not None else config.SMS_MAX_TRIES
    budget = total_budget if total_budget is not None else config.SMS_TOTAL_BUDGET
    wait_s = per_try_wait_s or config.SMS_WAIT_S

    @dataclass
    class A:
        order_id: str
        phone: str
        operator: str
        price: float
        status: str
        code: str = ""
        reason: str = ""

    history: list[A] = []
    spent = 0.0

    ABORT_MARKERS = ("RATE_LIMITED", "PHONE_PROVIDER_UNAVAILABLE",
                     "PHONE_ALREADY_VERIFIED", "PHONE_VERIFICATION_DISABLED",
                     "ONBOARDING_SESSION_EXPIRED", "TOO_MANY_REQUESTS")

    def emit(m, d=None):
        if on_event:
            on_event(m, d or {})

    def upstream_marker(txt: str):
        up = (txt or "").upper()
        for mk in ABORT_MARKERS:
            if mk in up:
                return mk
        return None

    def settle(order_id: str, why: str):
        """换号前把旧号退掉。等满激活期，别白扣钱。"""
        if not settle_before_retry:
            try:
                cancel(order_id)
            except Exception:
                pass
            return
        emit(f"{why} —— 等激活期满后退款（约 120s）", {"order": order_id})
        ok = cancel_when_allowed(order_id, on_event=emit)
        if not ok:
            emit("退款未成功，该号可能已计费", {"order": order_id})

    for attempt_no in range(1, tries + 1):
        if spent + config.SMS_MAX_PRICE > budget + 1e-9:
            raise HeroBudgetExceeded(f"接码预算用尽（已花 ${spent:.4f}/预算 ${budget}）")

        order = buy(c, svc, operator)      # 一次只开一个号
        spent += order.price
        a = A(order.id, order.phone, order.operator, order.price, "bought")
        history.append(a)
        emit(f"第 {attempt_no}/{tries} 次取号 {order.phone} ${order.price}", {"id": order.id})

        try:
            submit(order.phone)
        except Exception as e:
            a.status = "submit_failed"
            a.reason = f"{type(e).__name__}: {e}"
            mk = upstream_marker(str(e))
            if mk:
                settle(order.id, f"上游返回 {mk}，不再换号")
                raise HeroAbort(f"上游返回 {mk} —— 换号无用，已退单中止"
                                f"（本次花 ${spent:.4f}）。冷却后再试。") from e
            settle(order.id, f"号码被拒（{str(e)[:60]}）")
            continue

        try:
            code, _ = wait_code(order.id, timeout_s=wait_s)
        except HeroNoNumbers as e:
            a.status = "no_code"
            a.reason = str(e)
            settle(order.id, "未收到验证码")
            continue

        a.code = code
        try:
            ok = verify(code)
        except Exception as e:
            a.status = "verify_error"
            a.reason = f"{type(e).__name__}: {e}"
            mk = upstream_marker(str(e))
            if mk:
                settle(order.id, f"提交时上游返回 {mk}")
                raise HeroAbort(f"提交验证码时上游返回 {mk} —— 中止") from e
            settle(order.id, f"提交验证码异常（{str(e)[:50]}）")
            continue

        if ok:
            a.status = "verified"
            try:
                finish(order.id)
            except Exception:
                pass
            emit(f"验证成功（第 {attempt_no} 次，{order.phone}）", {"id": order.id})
            return code, a, history

        a.status = "rejected"
        a.reason = "目标站拒绝了该验证码"
        settle(order.id, "验证码被拒")
        emit("已换新号重试", {"id": order.id})

    raise HeroNoNumbers(f"连换 {tries} 个号都没成功（已花 ${spent:.4f}）。历史："
                        + "; ".join(f"#{x.order_id}:{x.status}" for x in history))
