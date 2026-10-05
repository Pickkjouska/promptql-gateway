# -*- coding: utf-8 -*-
"""
outlook.py —— Outlook refresh-token 邮箱通道

凭据格式（实测确认，**四段式**）:
    email----x----<refresh_token>----<client_id>
    第 2 段是占位（常为 'x'），不是密码；第 3 段才是 refresh token。

换取 token:
    POST https://login.microsoftonline.com/consumers/oauth2/v2.0/token
    data: client_id, grant_type=refresh_token, refresh_token,
          scope=https://graph.microsoft.com/.default      ← 必须 .default
    （用 Mail.Read 会 AADSTS70000）

读信:
    GET https://graph.microsoft.com/v1.0/me/messages
    /me 可能 401，但 /me/messages 正常 200
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

import requests

from .. import config
from .http import http

PLACEHOLDERS = {"x", "-", "n/a", "na", "none", "placeholder"}


class MailError(RuntimeError):
    pass


@dataclass
class MailCred:
    email: str
    refresh_token: str
    client_id: str
    password: str = ""

    def render(self) -> str:
        """四段式（推荐格式）。"""
        return (f"{self.email}----{self.password or 'x'}----"
                f"{self.refresh_token}----{self.client_id}")


def parse(raw: str) -> MailCred:
    """
    解析邮箱凭据。**推荐四段式**：

        email----password----refresh_token----client_id

    判据（不靠位置，靠形态）：
      - 第 1 段含 @ → email
      - 末段是 GUID → client_id
      - 倒数第 2 段以 M.C / 0.A / 1.A / Ew 开头（或很长）→ refresh_token
      - 中间剩下的 → password（可空，占位符也算空）

    也兼容旧的三段式 `email----refresh_token----client_id`。
    """
    s = raw.strip()
    if not s:
        raise MailError("空凭据")
    parts = [p.strip() for p in re.split(r"-{4,}", s)]
    if len(parts) < 3:
        raise MailError(f"段数={len(parts)}，至少 3 段")

    email = parts[0]
    if "@" not in email:
        raise MailError(f"第 1 段不像邮箱: {email[:40]!r}")

    cid = parts[-1]
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", cid):
        raise MailError(f"末段不是 GUID client_id: {cid[:40]!r}")

    # 从右往左找 refresh_token（跳过占位符）
    middle = parts[1:-1]
    rt, rt_idx = "", -1
    for idx in range(len(middle) - 1, -1, -1):
        v = middle[idx]
        if v.lower() in PLACEHOLDERS:
            continue
        if len(v) >= 40 or v.startswith(("M.C", "0.A", "1.A", "Ew")):
            rt, rt_idx = v, idx
            break
    if not rt:
        raise MailError("找不到 refresh_token 段")

    # 剩下的第一段当作 password（可能是 'x' 之类的占位符）
    pwd = ""
    for idx, v in enumerate(middle):
        if idx == rt_idx:
            continue
        if v.lower() in PLACEHOLDERS:
            continue
        pwd = v
        break

    return MailCred(email=email, refresh_token=rt, client_id=cid, password=pwd)


def access_token(cred: MailCred) -> tuple[str, int]:
    r = http.post(config.GRAPH_TOKEN_URL, data={
        "client_id": cred.client_id,
        "grant_type": "refresh_token",
        "refresh_token": cred.refresh_token,
        "scope": config.GRAPH_SCOPE,
    }, proxies=config.proxy_dict(), timeout=config.HTTP_TIMEOUT)
    j = r.json() if r.content else {}
    if r.status_code != 200 or "access_token" not in j:
        raise MailError(f"换 token 失败 HTTP {r.status_code}: "
                        f"{j.get('error_description') or j.get('error') or r.text[:200]}")
    return j["access_token"], int(j.get("expires_in", 3600))


def list_messages(tok: str, top: int = 25) -> list[dict]:
    r = http.get(f"{config.GRAPH}/me/messages", params={
        "$top": top,
        "$select": "id,subject,from,toRecipients,receivedDateTime,bodyPreview",
        "$orderby": "receivedDateTime desc",
    }, headers={"Authorization": f"Bearer {tok}"},
        proxies=config.proxy_dict(), timeout=config.HTTP_TIMEOUT)
    if r.status_code != 200:
        raise MailError(f"读信失败 HTTP {r.status_code}: {r.text[:200]}")
    return r.json().get("value", [])


def get_body(tok: str, mid: str) -> str:
    r = http.get(f"{config.GRAPH}/me/messages/{mid}",
                     params={"$select": "body"},
                     headers={"Authorization": f"Bearer {tok}"},
                     proxies=config.proxy_dict(), timeout=config.HTTP_TIMEOUT)
    try:
        return r.json().get("body", {}).get("content", "") or ""
    except Exception:
        return ""


_CODE_PATTERNS = [
    re.compile(r"sign[- ]?in code[^0-9]{0,40}(\d{4,8})", re.I),
    re.compile(r"code[^0-9]{0,20}(\d{4,8})", re.I),
    re.compile(r"\b(\d{6})\b"),
]


def extract_code(html: str) -> str | None:
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\s+", " ", text)
    for pat in _CODE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


def _parse_recv(s: str) -> float:
    """Graph 的 receivedDateTime 是 ISO8601（带 Z）。解析失败返回 0。"""
    if not s:
        return 0.0
    try:
        from datetime import datetime, timezone
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def fetch_otp(cred: MailCred, *, since_ts: float | None = None,
              exclude_ids: set[str] | None = None,
              sender_hint: str = config.OTP_SENDER_HINT,
              timeout_s: int = 180, poll_s: float = 5.0) -> tuple[str, dict]:
    """
    轮询收信取验证码。

    🔴 只返回 **发送时间晚于 since_ts** 的邮件 —— 这是必须的：
       同一账号历史邮件里很可能有旧的 PromptQL 验证码，
       拿旧码去提交会被上游判为无效，表现为「按钮一直 disabled / 点了没反应」。
    exclude_ids 用于额外排除已用过的邮件 id。

    返回 (code, message_meta)。
    """
    tok, _ = access_token(cred)
    deadline = time.time() + timeout_s
    excl = {x for x in (exclude_ids or set()) if x}
    seen_ids: set[str] = set()

    while time.time() < deadline:
        for m in list_messages(tok, top=15):
            mid = m.get("id") or ""
            frm = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "")
            subj = m.get("subject") or ""
            recv = m.get("receivedDateTime") or ""
            if sender_hint and sender_hint.lower() not in (frm + subj).lower():
                continue
            if mid in excl or mid in seen_ids:
                continue
            seen_ids.add(mid)
            if since_ts and _parse_recv(recv) and _parse_recv(recv) < since_ts:
                continue                      # ← 旧邮件，跳过
            body = get_body(tok, mid)
            code = extract_code(body) or extract_code(subj)
            if code:
                return code, {"id": mid, "from": frm, "subject": subj,
                              "received": recv, "code": code,
                              "recv_ts": _parse_recv(recv)}
        time.sleep(poll_s)
        tok, _ = access_token(cred)      # token 可能过期，刷新
    raise MailError(f"{timeout_s}s 内没等到来自 {sender_hint} 且晚于 "
                    f"{time.strftime('%H:%M:%S', time.localtime(since_ts)) if since_ts else '—'}"
                    f" 的验证码邮件（已排除 {len(excl)} 封旧码）")
