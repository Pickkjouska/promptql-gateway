# -*- coding: utf-8 -*-
"""
http.py —— 全局连接池

## 为什么需要（实测数据）

之前每个模块都用模块级的 `requests.post(...)` —— 那**每次都会新建 TCP + TLS 连接**。
实测：

    无连接池  requests.post     370 ms/次
    有连接池  requests.Session  241 ms/次
                                 ↑ 单次省 ~128 ms

看起来不多，但上游 agent 的等待期是**每 1.5s 轮询一次事件流**，
一次调用常常轮询 20~60 次 → **每次请求白等 2.5~7.7 秒**。
批量跑起来这个开销会成倍放大。

## 用法

    from .http import http
    r = http.post(url, json=..., timeout=30)
"""
from __future__ import annotations

import threading

import requests
from requests.adapters import HTTPAdapter

from .. import config

_local = threading.local()
_POOL = None
_LOCK = threading.Lock()


def _build(pool_size: int = 32) -> requests.Session:
    s = requests.Session()
    ad = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size,
                     max_retries=0,   # 重试由业务层决定（我们有自己的熔断/换号逻辑）
                     pool_block=False)
    s.mount("https://", ad)
    s.mount("http://", ad)
    # 默认头
    s.headers.update({
        "User-Agent": "promptql-gateway/1.0",
        "Accept-Encoding": "gzip, deflate",
    })
    return s


def _get_session() -> requests.Session:
    """
    每线程一个 Session。

    requests.Session **不是线程安全的**（内部 cookiejar / 连接池状态），
    而我们用线程池并发跑多账号，所以必须按线程隔离。
    """
    global _POOL
    s = getattr(_local, "session", None)
    if s is None:
        with _LOCK:
            s = _build()
        _local.session = s
    return s


class _HTTP:
    """带连接池的 requests 门面，签名与 requests 一致（proxy/timeout 自动带上默认值）。"""

    def _call(self, method: str, url: str, **kw):
        s = _get_session()
        # 代理：config 未设置则显式传 None（绕过环境变量里的 http_proxy）
        kw.setdefault("proxies", config.proxy_dict())
        kw.setdefault("timeout", config.HTTP_TIMEOUT)
        return getattr(s, method)(url, **kw)

    def get(self, url, **kw):
        return self._call("get", url, **kw)

    def post(self, url, **kw):
        return self._call("post", url, **kw)

    def put(self, url, **kw):
        return self._call("put", url, **kw)

    def request(self, method, url, **kw):
        return self._call(method.lower(), url, **kw)


http = _HTTP()


def warmup(urls: list[str]) -> None:
    """预热连接（建 TLS），用于服务启动时把首调延迟压下来。"""
    for u in urls:
        try:
            _get_session().head(u, timeout=5, allow_redirects=False)
        except Exception:
            pass
