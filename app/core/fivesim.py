# -*- coding: utf-8 -*-
"""
fivesim.py —— 5sim.net 接码客户端

接口（见 https://5sim.net/docs）:
    GET /v1/user/profile
    GET /v1/guest/prices?country=&product=
    GET /v1/user/buy/activation/{country}/{operator}/{product}
    GET /v1/user/check/{id}
    GET /v1/user/finish/{id}
    GET /v1/user/cancel/{id}

🔴 硬闸: 单价不得超过 config.FIVESIM_MAX_PRICE（默认 $0.10）。
    下单前先查价；查不到价或超价 → 直接拒绝，不发下单请求。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from .. import config


def normalize_phone(raw: str) -> str:
    """归一化成 E.164（带 + 前缀）。5sim/hero 都可能不带 +。"""
    import re as _re
    d = _re.sub(r"\D", "", str(raw or ""))
    return ("+" + d) if d else ""


class FiveSimError(RuntimeError):
    pass


class PriceExceeded(FiveSimError):
    """单价超闸。不重试，直接上报。"""


def _hdr() -> dict:
    k = config.fivesim_key()
    if not k:
        raise FiveSimError("没有 5sim API key（环境变量 FIVESIM / data/fivesim.key / secrets.json）")
    return {"Authorization": f"Bearer {k}", "Accept": "application/json"}


def _get(path: str, timeout=None):
    r = requests.get(f"{config.FIVESIM_BASE}{path}", headers=_hdr(),
                     proxies=config.proxy_dict(),
                     timeout=timeout or config.HTTP_TIMEOUT)
    if r.status_code == 401:
        raise FiveSimError("5sim 鉴权失败（key 过期或被吊销）")
    if r.status_code == 404:
        raise FiveSimError(f"5sim 无此资源: {path}")
    if r.status_code >= 400:
        raise FiveSimError(f"5sim HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except Exception:
        raise FiveSimError(f"5sim 返回非 JSON: {r.text[:200]}")


def profile() -> dict:
    return _get("/v1/user/profile")


def balance() -> float:
    return float(profile().get("balance", 0))


def prices(country: str | None = None, product: str | None = None) -> dict:
    """
    返回结构（实测）:  data[country][product][operator] = {cost, count, rate, ...}
    不传 product 则返回该国家全部产品。
    """
    c = country or config.FIVESIM_COUNTRY
    q = f"/v1/guest/prices?country={c}"
    if product and product not in ("any", ""):
        q += f"&product={product}"
    return _get(q, timeout=40)


def cheapest(country: str | None = None, product: str | None = None,
             max_price: float | None = None) -> list[dict]:
    """
    按单价升序返回可选档位。
    ⚠️ 实测结构是 [country][product][operator]，不是 [country][operator][product]。
    """
    cap = config.FIVESIM_MAX_PRICE if max_price is None else max_price
    c = country or config.FIVESIM_COUNTRY
    data = prices(c, product)
    rows = []
    for prod, ops in (data.get(c) or {}).items():
        for op, info in (ops or {}).items():
            cost = info.get("cost")
            if cost is None:
                continue
            rows.append({"country": c, "operator": op, "product": prod,
                         "cost": float(cost), "count": info.get("count") or 0,
                         "rate": info.get("rate"),
                         "under_cap": float(cost) <= cap + 1e-9})
    rows.sort(key=lambda r: (r["cost"], -r["count"]))
    return rows


def pick_cheapest(country: str | None = None, product: str | None = None) -> dict:
    """
    挑一个「有库存 且 单价在闸内」的最便宜档位。没有则抛 PriceExceeded。
    ⚠️ 优先选有库存的；若全地区都无库存，报错时带上「最便宜但缺货」的档位，
       便于人工判断是缺货还是超价。
    """
    cap = config.FIVESIM_MAX_PRICE
    allrows = cheapest(country, product)
    if not allrows:
        raise PriceExceeded(f"该地区没有可查到的档位（country={country or config.FIVESIM_COUNTRY}）")
    stocked = [r for r in allrows if r["count"] > 0]
    if not stocked:
        cheapest_row = allrows[0]
        raise PriceExceeded(
            f"该地区全部档位无库存（最便宜 ${cheapest_row['cost']} "
            f"{cheapest_row['operator']}/{cheapest_row['product']}）。"
            f"换地区或稍后重试 —— 不缺货时不会下单。")
    best = stocked[0]
    if not best["under_cap"]:
        raise PriceExceeded(
            f"有库存的最便宜档位 ${best['cost']} 超过闸值 ${cap} "
            f"({best['country']}/{best['operator']}/{best['product']}) —— 拒绝下单")
    return best


@dataclass
class Order:
    id: int
    phone: str
    operator: str
    product: str
    price: float
    status: str
    country: str = ""
    raw: dict | None = None


def parse_order(j: dict) -> Order:
    return Order(
        id=int(j.get("id")),
        phone=normalize_phone(j.get("phone")),
        operator=str(j.get("operator") or ""),
        product=str(j.get("product") or ""),
        price=float(j.get("price") or 0),
        status=str(j.get("status") or ""),
        country=str(j.get("country") or ""),
        raw=j,
    )


def buy(country: str | None = None, operator: str | None = None,
        product: str | None = None) -> Order:
    """
    下单。先做价格闸检查，再取号。
    不传 operator 时自动挑最便宜且在闸内的档位。
    """
    c = country or config.FIVESIM_COUNTRY
    p = product or config.FIVESIM_PRODUCT

    op = operator or config.FIVESIM_OPERATOR
    if operator:
        # 指定了运营商：仍要核价
        rows = [r for r in cheapest(c, p) if r["operator"] == op]
        if not rows:
            raise PriceExceeded(f"没有 {c}/{op}/{p} 这个档位")
        price = rows[0]["cost"]
        if price > config.FIVESIM_MAX_PRICE + 1e-9:
            raise PriceExceeded(f"{c}/{op}/{p} 单价 ${price} 超闸 ${config.FIVESIM_MAX_PRICE}，拒绝下单")
    else:
        best = pick_cheapest(c, p)
        op = best["operator"]

    j = _get(f"/v1/user/buy/activation/{c}/{op}/{p}", timeout=60)
    o = parse_order(j)
    # 二次核验: 实际扣费也必须在闸内
    if o.price > config.FIVESIM_MAX_PRICE + 1e-9:
        raise PriceExceeded(f"实际扣费 ${o.price} 超闸 ${config.FIVESIM_MAX_PRICE}（order={o.id}）")
    return o


def check(order_id: int) -> Order:
    return parse_order(_get(f"/v1/user/check/{order_id}", timeout=40))


def finish(order_id: int) -> Order:
    return parse_order(_get(f"/v1/user/finish/{order_id}", timeout=40))


def cancel(order_id: int) -> Order:
    return parse_order(_get(f"/v1/user/cancel/{order_id}", timeout=40))


_CODE_RE = None


def wait_code(order_id: int, timeout_s: int | None = None, poll_s: float = 5.0):
    """
    轮询等验证码。返回 (code, order)。
    5sim 的 sms 字段可能有多个验证码，取最后一个。
    """
    import re
    global _CODE_RE
    if _CODE_RE is None:
        _CODE_RE = re.compile(r"\b(\d{4,8})\b")

    deadline = time.time() + (timeout_s or config.FIVESIM_WAIT_S)
    last = None
    while time.time() < deadline:
        o = check(order_id)
        last = o
        sms = (o.raw or {}).get("sms") or []
        if sms:
            for item in reversed(sms):
                code = item.get("code")
                if code:
                    return str(code), o
                m = _CODE_RE.search(item.get("text") or "")
                if m:
                    return m.group(1), o
        if o.status in ("canceled", "banned", "timeout"):
            raise FiveSimError(f"订单状态 {o.status}，无验证码（order={order_id}）")
        time.sleep(poll_s)
    raise FiveSimError(f"{timeout_s or config.FIVESIM_WAIT_S}s 内没收到验证码"
                       f"（order={order_id}, 最后状态={(last.status if last else '?')}）")


class NoNumbersAvailable(FiveSimError):
    """这批号收不到码（虚拟号常态）。可换新号重试。"""


class BudgetExceeded(FiveSimError):
    """接码总预算用尽。停止换号。"""


class PhoneAbort(FiveSimError):
    """
    上游侧的问题（限流 / 手机服务故障 / 已绑定）—— **换号解决不了**。

    🔴 实测教训: 上游返回 429 RATE_LIMITED 时若继续换号，
       只会连续烧钱且大概率仍失败。这类错误必须立刻中止，等冷却后再来。
    """


@dataclass
class Attempt:
    order_id: int
    phone: str
    operator: str
    price: float
    status: str
    code: str = ""
    reason: str = ""


def buy_code_with_rotation(*, submit, verify,
                           country: str | None = None, product: str | None = None,
                           max_tries: int | None = None,
                           total_budget: float | None = None,
                           per_try_wait_s: int | None = None,
                           on_event=None) -> tuple[str, Attempt, list[Attempt]]:
    """
    「收不到码就换新号」的完整闭环。

    submit(phone) -> None        把号码提交给目标站（可能抛错）
    verify(code)  -> bool        提交验证码，返回是否通过
    返回 (最终 code, 成功的 Attempt, 全部尝试历史)

    纪律:
      - 每次失败先 cancel(order_id) 退单，再买新号（避免白扣费）
      - 总花费不得超过 total_budget，也不得超 FIVESIM_MAX_PRICE/单
      - max_tries 用尽即抛 NoNumbersAvailable，**不无限重试**
    """
    tries = max_tries if max_tries is not None else config.FIVESIM_MAX_TRIES
    budget = total_budget if total_budget is not None else config.FIVESIM_TOTAL_BUDGET
    wait_s = per_try_wait_s or config.FIVESIM_WAIT_S

    history: list[Attempt] = []
    spent = 0.0

    # 🔴 上游侧错误：换号无用，必须立刻停手（否则连续烧钱）
    ABORT_MARKERS = (
        "RATE_LIMITED", "PHONE_PROVIDER_UNAVAILABLE",
        "PHONE_ALREADY_VERIFIED", "PHONE_VERIFICATION_DISABLED",
        "ONBOARDING_SESSION_EXPIRED",
    )

    def emit(msg: str, data=None):
        if on_event:
            on_event(msg, data or {})

    def _submit_error_is_upstream(text: str) -> str | None:
        up = (text or "").upper()
        for mk in ABORT_MARKERS:
            if mk in up:
                return mk
        return None

    for attempt_no in range(1, tries + 1):
        if spent + config.FIVESIM_MAX_PRICE > budget + 1e-9:
            raise BudgetExceeded(
                f"接码预算用尽（已花 ${spent:.4f}，预算 ${budget}）—— 停止换号")

        best = pick_cheapest(country, product)
        emit(f"第 {attempt_no}/{tries} 次取号：{best['operator']}/{best['product']} "
             f"${best['cost']}", best)

        order = buy(country, best["operator"], best["product"])
        spent += order.price
        att = Attempt(order_id=order.id, phone=order.phone, operator=order.operator,
                      price=order.price, status="bought")
        history.append(att)

        try:
            submit(order.phone)
        except Exception as e:
            att.status = "submit_failed"
            att.reason = f"{type(e).__name__}: {e}"
            # 🔴 上游限流/故障：换号无用，退单后立刻中止整个流程
            mk = _submit_error_is_upstream(str(e))
            try:
                cancel(order.id)
                att.status = "canceled"
            except Exception:
                pass
            if mk:
                raise PhoneAbort(
                    f"上游返回 {mk} —— 换号解决不了，已在退单后中止"
                    f"（本次花 ${spent:.4f}）。请等几分钟冷却后重试。") from e
            emit(f"号码被目标站拒绝，换号重试：{e}", {"order": order.id})
            continue

        try:
            code, o = wait_code(order.id, timeout_s=wait_s)
        except FiveSimError as e:
            att.status = "no_code"
            att.reason = str(e)
            emit(f"未收到验证码，退单换号：{e}", {"order": order.id})
            try:
                cancel(order.id)
                att.status = "canceled"
            except Exception as ce:
                emit(f"退单失败（可能已计费）：{ce}", {"order": order.id})
            continue

        att.code = code
        try:
            ok = verify(code)
        except Exception as e:
            att.status = "verify_error"
            att.reason = f"{type(e).__name__}: {e}"
            emit(f"提交验证码异常：{e}", {"order": order.id})
            try:
                cancel(order.id)
            except Exception:
                pass
            continue

        if ok:
            att.status = "verified"
            try:
                finish(order.id)
            except Exception:
                pass
            emit(f"验证成功（第 {attempt_no} 次，号码 {order.phone}）",
                 {"order": order.id})
            return code, att, history

        att.status = "rejected"
        att.reason = "目标站拒绝了该验证码"
        emit("验证码被拒，退单换号", {"order": order.id})
        try:
            cancel(order.id)
            att.status = "canceled"
        except Exception:
            pass

    raise NoNumbersAvailable(
        f"连换 {tries} 个号都没成功（已花 ${spent:.4f}）。"
        f"历史：" + "; ".join(f"#{a.order_id}:{a.status}" for a in history))
