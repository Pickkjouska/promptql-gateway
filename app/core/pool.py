# -*- coding: utf-8 -*-
"""
pool.py —— 账号池 + 轮询调度

职责:
  - 维护每个账号的 Session（惰性建、按 exp 自动续期）
  - 健康检查（cookie 是否仍有效、余额/额度）
  - 选号策略: 优先健康 + 额度充足 + 最近最少使用
  - 失败退避（连续失败标记 unhealthy，冷却后重试）
"""
from __future__ import annotations

import os
import threading
import time

from .. import config
from . import store
from .promptql import AuthExpired, PaymentRequired, Session, UpstreamError

_LOCK = threading.Lock()
_SESS: dict[str, Session] = {}
_COOLDOWN: dict[str, float] = {}       # email -> 可再次使用的时间戳
_FAILS: dict[str, int] = {}
_LAST_USED: dict[str, float] = {}

MAX_FAILS = int(getattr(config, "POOL_MAX_FAILS", 3))
COOLDOWN_S = int(getattr(config, "POOL_COOLDOWN_S", 120))


def session_for(email: str, force: bool = False) -> Session:
    with _LOCK:
        s = _SESS.get(email)
        if s is None or force:
            sec = store.get_secret(email)
            if not sec.get("cookie"):
                raise AuthExpired(f"{email} 没有 cookie，需先注册/导入")
            acct = store.get_account(email) or {}
            s = Session(cookie=sec["cookie"], email=email,
                        project_id=(acct.get("project_id") or ""))
            _SESS[email] = s
        # 建好项目后落库，下次直接复用，避免每次都去 list/create
        if s.project_id:
            try:
                if (store.get_account(email) or {}).get("project_id") != s.project_id:
                    store.upsert_account(email, project_id=s.project_id,
                                         pql_user_id=s.pql_user_id or None)
            except Exception:
                pass
        return s


def invalidate(email: str):
    with _LOCK:
        _SESS.pop(email, None)


def mark_fail(email: str, err: str, *, cooldown: bool = True):
    with _LOCK:
        _FAILS[email] = _FAILS.get(email, 0) + 1
        if cooldown and _FAILS[email] >= MAX_FAILS:
            _COOLDOWN[email] = time.time() + COOLDOWN_S
    store.log("warn", "pool", f"失败第 {_FAILS.get(email)} 次: {err[:160]}", email)


def mark_ok(email: str):
    with _LOCK:
        _FAILS[email] = 0
        _COOLDOWN.pop(email, None)


# 已知没有额度的账号（会被 pick 跳过）
_NO_QUOTA: set[str] = set()


def mark_no_quota(email: str, reason: str = ""):
    """标记账号无额度 —— 别再往上发请求，那只会白等 180s 超时。"""
    with _LOCK:
        _NO_QUOTA.add(email)
    store.log("warn", "pool", f"标记为无额度，暂停使用: {reason[:120]}", email)


def clear_no_quota(email: str):
    with _LOCK:
        _NO_QUOTA.discard(email)


def _available(rows: list[dict]) -> list[dict]:
    now = time.time()
    out = []
    for r in rows:
        e = r["email"]
        if r.get("status") in ("blocked_card", "failed"):
            continue
        if not r.get("has_cookie"):
            continue
        if e in _NO_QUOTA:
            # 🔴 无额度账号：发过去只会让 agent 干等 → 180s 超时。
            #    实测这是「连接不顺畅 / 老是超时」的主因。
            continue
        if r.get("olu_total") is not None and r.get("olu_used") is not None:
            try:
                if float(r["olu_total"]) > 0 and float(r["olu_used"]) >= float(r["olu_total"]):
                    continue
            except Exception:
                pass
        if _COOLDOWN.get(e, 0) > now:
            continue
        out.append(r)
    # 最近最少使用优先
    out.sort(key=lambda r: _LAST_USED.get(r["email"], 0))
    return out


def snapshot() -> list[dict]:
    """给前端看的状态快照。"""
    rows = store.list_accounts()
    now = time.time()
    out = []
    for r in rows:
        e = r["email"]
        cd = _COOLDOWN.get(e, 0)
        out.append({
            "email": e,
            "status": r.get("status"),
            "has_cookie": bool(r.get("has_cookie")),
            "pql_user_id": r.get("pql_user_id"),
            "olu_total": r.get("olu_total"),
            "olu_used": r.get("olu_used"),
            "phone": r.get("phone"),
            "fails": _FAILS.get(e, 0),
            "cooldown_s": max(0, int(cd - now)) if cd > now else 0,
            "last_used": _LAST_USED.get(e),
            "note": (r.get("note") or "")[:160],
        })
    return out


def pick(exclude: set[str] | None = None) -> str | None:
    ex = exclude or set()
    for r in _available(store.list_accounts()):
        if r["email"] not in ex:
            return r["email"]
    return None


def call_any(prompt: str, *, model: str | None = None, exclude: set[str] | None = None,
             on_delta=None, **kw):
    """
    选一个健康的账号发请求。失败自动换号（最多试遍池子）。
    返回 (CallResult, email)。
    """
    from .promptql import call as pql_call

    # 只把 pull 认得的参数透传给上游；apikey/protocol 是本层记账用的
    apikey = kw.pop("apikey", None)
    protocol = kw.pop("protocol", "internal")

    # 🔴 geo_blocked 是**出口 IP 层面**的限制，换账号没用 —— 换号只会
    #    白白耗尽整个池子并让请求挂很久（实测：一次调用把 5 个账号全试一遍）。
    #    正确做法是**原地退避重试**，给出口一点时间。
    GEO_RETRY = int(os.getenv("PQL_GEO_RETRY", "3"))
    GEO_BACKOFF = float(os.getenv("PQL_GEO_BACKOFF", "2.5"))
    geo_fails = 0

    tried: set[str] = set(exclude or set())
    last_err: Exception | None = None
    while True:
        email = pick(tried)
        if not email:
            raise UpstreamError(f"池子里没有可用账号（试过 {len(tried)} 个）: {last_err}")
        tried.add(email)
        try:
            s = session_for(email)
            res = pql_call(s, prompt, model=model, on_delta=on_delta, **kw)
            mark_ok(email)
            # 首次使用可能刚建好项目，回写便于下次复用
            if s.project_id and (store.get_account(email) or {}).get("project_id") != s.project_id:
                store.upsert_account(email, project_id=s.project_id)
            _LAST_USED[email] = time.time()
            store.record_usage(account_id=(store.get_account(email) or {}).get("id"),
                               model=res.model, thread_id=res.thread_id,
                               protocol=protocol,
                               input_tokens=res.input_tokens,
                               output_tokens=res.output_tokens,
                               cached_tokens=res.cached_tokens,
                               latency_ms=res.latency_ms, ttft_ms=res.ttft_ms,
                               project_id=s.project_id, account_email=email,
                               apikey_label=store.apikey_label(apikey),
                               ok=True, apikey=apikey)
            return res, email
        except PaymentRequired as e:
            mark_fail(email, f"银行卡熔断 {e}")
            store.upsert_account(email, status="blocked_card", note=str(e)[:300])
            last_err = e
        except AuthExpired as e:
            invalidate(email)
            mark_fail(email, f"cookie 失效 {e}")
            store.upsert_account(email, status="cookie_expired")
            last_err = e
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if "geo_blocked" in msg:
                geo_fails += 1
                tried.discard(email)          # 不算这个账号的错，还回去
                if geo_fails <= GEO_RETRY:
                    store.log("warn", "pool",
                              f"出口被地区限制，{GEO_BACKOFF}s 后重试"
                              f"（{geo_fails}/{GEO_RETRY}）", email)
                    time.sleep(GEO_BACKOFF * geo_fails)
                    continue
                raise UpstreamError(
                    f"出口 IP 被上游地区限制（geo_blocked），已原地退避 {GEO_RETRY} 次仍失败。"
                    f"这不是账号问题 —— 请更换代理节点后重试。原文: {msg[:160]}") from e
            mark_fail(email, msg)
            # 等待超时 + 该账号额度为 0/未知 → 判定无额度，从池里摘掉
            try:
                acct = store.get_account(email) or {}
                tot = acct.get("olu_total")
                if "等待超时" in msg and (tot in (None, 0, "0", 0.0)):
                    mark_no_quota(email, msg)
            except Exception:
                pass
            try:
                store.record_usage(account_id=(store.get_account(email) or {}).get("id"),
                                   protocol=protocol, model=str(model or ""), ok=False,
                                   err=f"{type(e).__name__}: {str(e)[:200]}",
                                   apikey=apikey, apikey_label=store.apikey_label(apikey),
                                   account_email=email)
            except Exception:
                pass
            last_err = e
