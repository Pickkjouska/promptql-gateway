#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py —— PromptQL MCP Server

把网关能力包成 MCP 工具，给 ZCode / Codex / Claude Code 调用。

## 架构（为什么 MCP 形态最好）

PromptQL 是**封闭 agent**：不接受外部工具定义，动作写死在服务端。
所以它不能当 Claude Code 的模型后端（那边需要模型回 tool_use）。

但 MCP 的工具是**服务端定义**的 —— 我定义 pql_code(path, task)，
客户端调它，内部读文件 → 喂给 agent → 写回磁盘 → 返回结果。

    客户端 → pql_code(path, task) → 本 Server
                                      ↓
                        读你磁盘 → PromptQL agent(Opus 5.5)
                                      ↓
                              写回磁盘 → 返回摘要
                                      ↓
    客户端 ←────────────────────────┘

## 长任务（🔴 关键）

ZCode 的 MCP 调用超时是 60s，而 agent 复杂任务要几分钟。
所以长任务用 **异步工具**：
    pql_code_async(...)  → 立刻返回 task_id
    pql_task(task_id)    → 轮询结果

## 起法

    python mcp_server/server.py        # stdio
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ============================================================
# 🔴 必须在导入 fastmcp **之前**设置。
#    FastMCP 默认往 stderr 打 ASCII 横幅 + 更新提示 + INFO 日志，
#    很多 MCP 客户端（ZCode 就是）看到 stderr 有输出就判定「连接失败」，
#    哪怕握手其实成功了。关掉，保持 stderr 干净。
# ============================================================
os.environ.setdefault("FASTMCP_SHOW_SERVER_BANNER", "false")
os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")   # 只接受 stable/prerelease/off
os.environ.setdefault("FASTMCP_LOG_ENABLED", "false")
os.environ.setdefault("FASTMCP_ENABLE_RICH_LOGGING", "false")
os.environ.setdefault("FASTMCP_LOG_LEVEL", "ERROR")

import json          # noqa: E402
import logging       # noqa: E402
import threading     # noqa: E402
import time          # noqa: E402
import uuid          # noqa: E402

from fastmcp import FastMCP  # noqa: E402

from app import config  # noqa: E402
from app.core import pool, store, workspace  # noqa: E402

try:
    mcp = FastMCP("promptql", show_server_banner=False)
except TypeError:
    mcp = FastMCP("promptql")

logging.basicConfig(level=logging.ERROR, force=True)
for _n in ("fastmcp", "mcp", "asyncio"):
    logging.getLogger(_n).setLevel(logging.ERROR)

NL = chr(10)
DEFAULT_MODEL = os.getenv("PQL_MCP_MODEL", "claude opus 5.5")


# ==================== 后台任务表 ====================

_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = int(os.getenv("PQL_MCP_JOB_TTL", "3600"))


def _new_job(kind: str, summary: str) -> str:
    jid = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[jid] = {"id": jid, "kind": kind, "status": "running",
                      "summary": summary, "started": time.time(),
                      "result": "", "error": "", "meta": {}}
    return jid


def _job_done(jid: str, result: str, meta: dict | None = None):
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        if j:
            j.update(status="done", result=result, finished=time.time(),
                     meta=meta or {})


def _job_fail(jid: str, err: str):
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        if j:
            j.update(status="failed", error=err, finished=time.time())


def _gc_jobs():
    now = time.time()
    with _JOBS_LOCK:
        for k in [k for k, v in _JOBS.items()
                  if v.get("finished") and now - v["finished"] > _JOB_TTL]:
            _JOBS.pop(k, None)


# ==================== 同步工具（快任务）====================

@mcp.tool()
def pql_ask(prompt: str, model: str = DEFAULT_MODEL) -> str:
    """
    问 PromptQL（Claude Opus 5.5 等）一个问题，不接触本地文件。

    适合快速咨询。如果任务复杂（可能超过 1 分钟），改用 pql_ask_async。

    Args:
        prompt: 你的问题或需求
        model:  模型名："claude opus 5.5" / "claude fable 5.1" /
                "gpt-6.1 sol" / "gpt-6 astra"
    """
    try:
        res, _ = pool.call_any(prompt, model=model, protocol="mcp")
        return res.text or "(空回复)"
    except Exception as e:
        return "[失败] %s: %s" % (type(e).__name__, e)


@mcp.tool()
def pql_files(sub: str = "", limit: int = 200) -> str:
    """
    列出本地工作区里的文件。

    Args:
        sub:   子目录（留空列全部）
        limit: 最多返回多少个
    """
    try:
        fs = workspace.list_files(sub, limit)
        if not fs:
            return "（空）工作区根目录: %s" % config.WORKSPACE_ROOT
        lines = ["工作区: %s" % config.WORKSPACE_ROOT,
                 "共 %d 个文件:" % len(fs)]
        lines += ["  %s  (%dB)" % (f["path"], f["size"]) for f in fs]
        return NL.join(lines)
    except Exception as e:
        return "[失败] %s: %s" % (type(e).__name__, e)


@mcp.tool()
def pql_read(path: str) -> str:
    """
    读一个本地文件的内容。

    Args:
        path: 相对工作区的路径
    """
    try:
        fb = workspace.read_file(path)
        head = "=== %s (%dB%s) ===" % (
            fb.path, fb.size, "  已截断" if fb.truncated else "")
        return head + NL + fb.content
    except Exception as e:
        return "[失败] %s: %s" % (type(e).__name__, e)


@mcp.tool()
def pql_write(path: str, content: str) -> str:
    """
    写一个本地文件（**会直接改你的磁盘**，确认后再用）。

    Args:
        path:    相对工作区的路径
        content: 完整文件内容
    """
    try:
        r = workspace.write_file(path, content, allow_write=True)
        store.log("info", "mcp", "手动写入 %s (%dB)" % (r["path"], r["bytes"]))
        return "已写入 %s (%dB)" % (r["path"], r["bytes"])
    except Exception as e:
        return "[失败] %s: %s" % (type(e).__name__, e)


@mcp.tool()
def pql_pool() -> str:
    """查看账号池状态（哪些账号可用、额度、故障）。"""
    try:
        rows = pool.snapshot()
        if not rows:
            return "账号池为空"
        lines = ["共 %d 个账号:" % len(rows)]
        for r in rows:
            olu = ("%s/%s" % (r.get("olu_used"), r.get("olu_total"))
                   if r.get("olu_total") else "—")
            lines.append("  %-38s %-10s OLU=%-16s 失败=%s" % (
                r["email"][:38], r["status"], olu, r.get("fails", 0)))
        return NL.join(lines)
    except Exception as e:
        return "[失败] %s: %s" % (type(e).__name__, e)


# ==================== 异步工具（长任务）====================

@mcp.tool()
def pql_code_async(path: str, task: str, write: bool = False,
                   include_tree: bool = False, model: str = DEFAULT_MODEL,
                   extra_files: str = "") -> str:
    """
    **长任务专用**：提交代码任务到后台，立刻返回 task_id，不阻塞。

    🔴 复杂任务（多文件重构、深度审计）agent 要跑几分钟，
       而客户端单次 MCP 调用常常只有 60s 超时 —— 同步版必然被掐断。
       用这个：立刻拿 task_id，再用 pql_task(task_id) 轮询。

    Args:
        path:         相对工作区的文件或目录
        task:         要它做什么
        write:        是否写回磁盘（默认 False，只给建议）
        include_tree: 附带工作区文件树
        model:        模型名
        extra_files:  额外读入的文件（逗号分隔）
    """
    try:
        files, _ = _collect(path)
        if not files:
            return "[失败] 路径下没有文件: %s" % path
        if extra_files:
            files += [f.strip() for f in extra_files.split(",") if f.strip()]
    except Exception as e:
        return "[失败] %s: %s" % (type(e).__name__, e)

    jid = _new_job("code", "%s  (%d 文件)" % (task[:60], len(files)))

    def worker():
        try:
            prompt = workspace.build_prompt(
                task, files=files, include_tree=include_tree,
                root_hint=str(config.WORKSPACE_ROOT))
            res, _ = pool.call_any(prompt, model=model, protocol="mcp-async")

            applied = []
            if write:
                if workspace.parse_file_blocks(res.text):
                    applied = workspace.apply_file_blocks(res.text, allow_write=True)
                    okn = sum(1 for a in applied if a.get("ok"))
                    store.log("info", "mcp",
                              "[异步] 写回 %d/%d 文件" % (okn, len(applied)),
                              data=applied)

            out = ["[模型] %s  [读入] %d 个文件" % (res.model, len(files))]
            if applied:
                ok = [a for a in applied if a.get("ok")]
                out.append("[写回] %d 成功 / %d 个文件块" % (len(ok), len(applied)))
                for a in ok:
                    out.append("   ✓ %s (%dB)" % (a["path"], a["bytes"]))
            elif write:
                out.append("[写回] 模型未输出 <<<FILE:>>> 块，无改动")
            else:
                out.append("[只读模式] 未写回磁盘")
            if res.tools:
                out.append("[工具轨迹] %d 条" % len(res.tools))
            out.append("")
            out.append(res.text or "(空回复)")
            _job_done(jid, NL.join(out),
                      {"model": res.model, "files": len(files),
                       "applied": applied, "tools": len(res.tools or [])})
        except Exception as e:
            _job_fail(jid, "%s: %s" % (type(e).__name__, e))

    threading.Thread(target=worker, daemon=True).start()
    return ("已提交后台任务。" + NL +
            "task_id = %s" % jid + NL +
            "摘要   = %s" % task[:80] + NL +
            "用 pql_task(\"%s\") 轮询结果（建议 10~30s 一次）。" % jid)


@mcp.tool()
def pql_ask_async(prompt: str, model: str = DEFAULT_MODEL) -> str:
    """
    **长任务专用**：把问题提交到后台，立刻返回 task_id。

    Args:
        prompt: 你的问题
        model:  模型名
    """
    jid = _new_job("ask", prompt[:60])

    def worker():
        try:
            res, _ = pool.call_any(prompt, model=model, protocol="mcp-async")
            _job_done(jid, res.text or "(空回复)",
                      {"model": res.model, "tools": len(res.tools or [])})
        except Exception as e:
            _job_fail(jid, "%s: %s" % (type(e).__name__, e))

    threading.Thread(target=worker, daemon=True).start()
    return ("已提交后台任务。" + NL +
            "task_id = %s" % jid + NL +
            "用 pql_task(\"%s\") 轮询结果。" % jid)


@mcp.tool()
def pql_task(task_id: str, wait_s: int = 0) -> str:
    """
    查后台任务状态 / 取结果。

    Args:
        task_id: pql_code_async / pql_ask_async 返回的 id
        wait_s:  可选等待秒数（最多 50，避免超客户端 60s 超时）。0 = 立即返回
    """
    _gc_jobs()
    deadline = time.time() + max(0, min(wait_s, 50))
    snap = None
    while True:
        with _JOBS_LOCK:
            j = _JOBS.get(task_id)
            if j is None:
                return ("[未找到] task_id=%s（可能已过期或拼错）" % task_id) + NL + \
                       "用 pql_tasks() 看所有任务。"
            snap = dict(j)
        if snap["status"] != "running" or time.time() >= deadline:
            break
        time.sleep(2)

    st = snap["status"]
    el = time.time() - snap["started"]
    head = "[%s] task=%s  已运行 %.0fs  类型=%s" % (st, task_id, el, snap["kind"])
    if snap.get("summary"):
        head += NL + "摘要: " + snap["summary"]

    if st == "running":
        return head + NL + NL + "（仍在跑。过一会再调 pql_task 查询。）"
    if st == "failed":
        return head + NL + NL + "[错误] " + snap["error"]
    meta = snap.get("meta") or {}
    if meta:
        head += NL + "元信息: " + json.dumps(meta, ensure_ascii=False)[:300]
    return head + NL + NL + (snap.get("result") or "(空)")


@mcp.tool()
def pql_tasks() -> str:
    """列出所有后台任务（含已完成未过期的）。"""
    _gc_jobs()
    with _JOBS_LOCK:
        items = sorted(_JOBS.values(), key=lambda x: x["started"], reverse=True)
    if not items:
        return "没有后台任务。"
    lines = ["共 %d 个任务:" % len(items)]
    for j in items:
        el = (j.get("finished") or time.time()) - j["started"]
        lines.append("  %s  [%-7s] %6.0fs  %-5s  %s" % (
            j["id"], j["status"], el, j["kind"], j["summary"][:50]))
    return NL.join(lines)


# ==================== 辅助 ====================

def _collect(path: str) -> tuple[list[str], str]:
    """把路径展开成 (文件列表, 根名)。"""
    root = Path(config.WORKSPACE_ROOT)
    p = (root / path).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise workspace.WorkspaceError("路径越界: %s" % path)
    if p.is_file():
        return [str(p.relative_to(root)).replace("\\", "/")], str(p.parent)
    if not p.exists():
        raise workspace.WorkspaceError("路径不存在: %s" % path)
    files = []
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
    for dirpath, dirnames, filenames in os.walk(p):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            fp = Path(dirpath) / fn
            files.append(str(fp.relative_to(root)).replace("\\", "/"))
    return files, str(p)


if __name__ == "__main__":
    mcp.run()
