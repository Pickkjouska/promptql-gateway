# -*- coding: utf-8 -*-
"""
store.py —— SQLite 存储层 + 凭据加密

表:
  accounts      邮箱账号(含加密的 RT/密码)与注册状态、所属出口
  cookies       账号的 hasura-lux cookie 与 JWT 缓存
  apikeys       下游自建 API Key
  usage         每次调用的 token 用量与耗时
  events        日志/审计（注册、接码、熔断、错误）
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .. import config

_LOCK = threading.Lock()
_conn: sqlite3.Connection | None = None
_fernet: Fernet | None = None


def _master_key() -> bytes:
    """主密钥: 环境变量 APP_MASTER_KEY > data/master.key > 首次生成"""
    if config.MASTER_KEY:
        k = config.MASTER_KEY.encode()
        return k if len(k) == 44 else Fernet.generate_key()
    f = config.DATA / "master.key"
    if f.exists():
        return f.read_bytes().strip()
    config.DATA.mkdir(parents=True, exist_ok=True)
    k = Fernet.generate_key()
    f.write_bytes(k)
    try:
        f.chmod(0o600)
    except Exception:
        pass
    return k


def fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_master_key())
    return _fernet


def enc(s: str) -> str:
    if not s:
        return ""
    return fernet().encrypt(s.encode()).decode()


def dec(s: str) -> str:
    if not s:
        return ""
    try:
        return fernet().decrypt(s.encode()).decode()
    except (InvalidToken, Exception):
        return ""


SCHEMA = """
create table if not exists accounts (
  id            integer primary key autoincrement,
  email         text unique not null,
  rt_enc        text,                -- 加密的 refresh_token
  client_id     text,
  password_enc  text,                -- 可选的 password 位
  status        text default 'new',  -- new/logged_in/needs_phone/phone_done/active/failed/blocked_card
  cookie_enc    text,                -- 加密的 hasura-lux
  pql_user_id   text,
  ctrl_user_id  text,
  project_id    text,                -- 🔴 该账号自己的项目（enrich_token 必须用它）
  balance_usd   real,
  olu_total     real,
  olu_used      real,
  phone         text,
  note          text,
  created_at    real,
  updated_at    real
);

create table if not exists apikeys (
  id          integer primary key autoincrement,
  key         text unique not null,   -- 下游调用用
  label       text,
  enabled     integer default 1,
  created_at  real
);

create table if not exists usage (
  id           integer primary key autoincrement,
  ts           real,
  apikey       text,
  account_id   integer,
  protocol     text,     -- chat/responses/messages
  model        text,
  thread_id    text,
  input_tokens integer,
  output_tokens integer,
  cached_tokens integer,
  latency_ms   integer,
  ok           integer,
  err          text,
  ttft_ms      integer,     -- 首字延迟（毫秒）
  apikey_label text,        -- 下游 Key 的备注名，便于区分是哪个 token 在用
  project_id   text,
  account_email text
);

create table if not exists events (
  id      integer primary key autoincrement,
  ts      real,
  level   text,       -- info/warn/error
  scope   text,       -- register/phone/gateway/invite/auth
  account text,
  msg     text,
  data    text
);
"""


# 增量迁移：老库不会因为 create table if not exists 自动加列
MIGRATIONS = [
    ("accounts", "project_id", "text"),
    ("usage", "ttft_ms", "integer"),
    ("usage", "apikey_label", "text"),
    ("usage", "project_id", "text"),
    ("usage", "account_email", "text"),
]


def _migrate(c: sqlite3.Connection) -> None:
    for table, col, typ in MIGRATIONS:
        try:
            cols = {r[1] for r in c.execute(f"pragma table_info({table})")}
            if col not in cols:
                c.execute(f"alter table {table} add column {col} {typ}")
        except Exception:
            pass


def conn() -> sqlite3.Connection:
    global _conn
    with _LOCK:
        if _conn is None:
            config.DATA.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(SCHEMA)
            _migrate(_conn)
            _conn.commit()
        return _conn


@contextmanager
def tx():
    c = conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


def log(level: str, scope: str, msg: str, account: str = "", data=None):
    with tx() as c:
        c.execute("insert into events(ts,level,scope,account,msg,data) values(?,?,?,?,?,?)",
                  (time.time(), level, scope, account, msg,
                   json.dumps(data, ensure_ascii=False) if data is not None else None))


# ---------- accounts ----------

def upsert_account(email: str, **fields) -> int:
    email = email.strip().lower()
    with tx() as c:
        row = c.execute("select id from accounts where email=?", (email,)).fetchone()
        if row:
            sid = row["id"]
            if fields:
                sets, vals = [], []
                for k, v in fields.items():
                    sets.append(f"{k}=?")
                    vals.append(v)
                sets.append("updated_at=?")
                vals += [time.time(), sid]
                c.execute(f"update accounts set {','.join(sets)} where id=?", vals)
            return sid
        cols = ["email", "created_at", "updated_at"] + list(fields)
        vals = [email, time.time(), time.time()] + list(fields.values())
        cur = c.execute(f"insert into accounts({','.join(cols)}) values({','.join('?' * len(cols))})",
                        vals)
        return cur.lastrowid


def get_account(email_or_id) -> dict | None:
    with tx() as c:
        if isinstance(email_or_id, int) or str(email_or_id).isdigit():
            row = c.execute("select * from accounts where id=?", (int(email_or_id),)).fetchone()
        else:
            row = c.execute("select * from accounts where email=?",
                            (str(email_or_id).strip().lower(),)).fetchone()
    return dict(row) if row else None


def delete_account(email: str, purge_usage: bool = True) -> dict:
    """
    删除账号及其关联数据。

    🔴 不可逆，调用方必须先确认。
    清掉: accounts 行（含加密的 cookie / refresh_token）+ usage 里该账号的记录。
    events 表**保留** —— 审计日志不该因为删号而消失。

    返回各表实际删除行数，供前端如实回显。
    """
    email = email.strip().lower()
    out = {"email": email, "account": 0, "usage": 0}
    with tx() as c:
        row = c.execute("select id from accounts where email=?", (email,)).fetchone()
        if not row:
            return out
        aid = row["id"]
        out["account"] = c.execute("delete from accounts where id=?", (aid,)).rowcount
        if purge_usage:
            out["usage"] = c.execute(
                "delete from usage where account_id=? "
                "or lower(coalesce(account_email,''))=?",
                (aid, email)).rowcount
    return out


def list_accounts() -> list[dict]:
    with tx() as c:
        rows = c.execute("select * from accounts order by id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # 不外泄密文
        d.pop("rt_enc", None)
        d.pop("cookie_enc", None)
        d.pop("password_enc", None)
        d["has_cookie"] = bool(r["cookie_enc"])
        out.append(d)
    return out


def set_secret(email: str, *, rt: str | None = None, client_id: str | None = None,
               password: str | None = None, cookie: str | None = None):
    f = {}
    if rt is not None:
        f["rt_enc"] = enc(rt)
    if client_id is not None:
        f["client_id"] = client_id
    if password is not None:
        f["password_enc"] = enc(password)
    if cookie is not None:
        f["cookie_enc"] = enc(cookie)
    upsert_account(email, **f)


def get_secret(email: str) -> dict:
    a = get_account(email)
    if not a:
        return {}
    return {
        "rt": dec(a.get("rt_enc") or ""),
        "client_id": a.get("client_id") or "",
        "password": dec(a.get("password_enc") or ""),
        "cookie": dec(a.get("cookie_enc") or ""),
    }


# ---------- apikeys ----------

def create_apikey(label: str = "") -> str:
    import secrets
    key = "pql-" + secrets.token_urlsafe(32)
    with tx() as c:
        c.execute("insert into apikeys(key,label,enabled,created_at) values(?,?,1,?)",
                  (key, label, time.time()))
    return key


def list_apikeys() -> list[dict]:
    with tx() as c:
        return [dict(r) for r in c.execute(
            "select * from apikeys order by id desc").fetchall()]


def apikey_label(key: str | None) -> str:
    """取下游 Key 的备注名，用于用量记录里区分是哪个 token 在用。"""
    if not key:
        return ""
    with tx() as c:
        r = c.execute("select label from apikeys where key=?", (key,)).fetchone()
    return (r["label"] if r and r["label"] else "") or f"key:{key[:10]}"


def apikey_ok(key: str) -> bool:
    if not key:
        return False
    with tx() as c:
        r = c.execute("select enabled from apikeys where key=?", (key,)).fetchone()
    return bool(r and r["enabled"])


def revoke_apikey(key: str):
    with tx() as c:
        c.execute("update apikeys set enabled=0 where key=?", (key,))


# ---------- usage / events ----------

def record_usage(**kw):
    with tx() as c:
        c.execute("""insert into usage(ts,apikey,account_id,protocol,model,thread_id,
                     input_tokens,output_tokens,cached_tokens,latency_ms,ok,err,
                     ttft_ms,apikey_label,project_id,account_email)
                     values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (time.time(), kw.get("apikey"), kw.get("account_id"), kw.get("protocol"),
                   kw.get("model"), kw.get("thread_id"), kw.get("input_tokens", 0),
                   kw.get("output_tokens", 0), kw.get("cached_tokens", 0),
                   kw.get("latency_ms", 0), 1 if kw.get("ok") else 0, kw.get("err"),
                   kw.get("ttft_ms"), kw.get("apikey_label"), kw.get("project_id"),
                   kw.get("account_email")))


def usage_summary(limit: int = 200) -> dict:
    with tx() as c:
        tot = c.execute("""select count(*) n,
                              coalesce(sum(input_tokens),0) inp,
                              coalesce(sum(output_tokens),0) outp,
                              coalesce(sum(cached_tokens),0) cache,
                              coalesce(sum(ok),0) okn
                           from usage""").fetchone()
        recent = [dict(r) for r in c.execute(
            "select * from usage order by id desc limit ?", (limit,)).fetchall()]
        by_model = [dict(r) for r in c.execute(
            """select model, count(*) n, sum(input_tokens) inp, sum(output_tokens) outp
               from usage group by model order by n desc""").fetchall()]
        by_key = [dict(r) for r in c.execute(
            """select coalesce(apikey_label, substr(apikey,1,14)) k,
                      count(*) n, sum(input_tokens) inp, sum(output_tokens) outp,
                      avg(latency_ms) avg_ms, avg(ttft_ms) avg_ttft
               from usage where apikey is not null group by k order by n desc""").fetchall()]
    return {"total": dict(tot), "recent": recent, "by_model": by_model, "by_key": by_key}


def recent_events(limit: int = 200) -> list[dict]:
    with tx() as c:
        return [dict(r) for r in c.execute(
            "select * from events order by id desc limit ?", (limit,)).fetchall()]
