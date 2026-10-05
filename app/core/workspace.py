# -*- coding: utf-8 -*-
"""
workspace.py —— 本地工作区桥（让远程 agent 能读写你本地的代码）

## 为什么需要

PromptQL 的 agent 有自己的沙箱（`run_shell` / `write_file` 都跑在它的 Linux VM 里），
**够不到你本机的文件**。但**网关就跑在你本机** —— 所以由网关替它做 I/O：

    本地文件 ──读取──> 网关 ──拼进 prompt──> agent（只负责"想"）
                                             │
    本地磁盘 <──写回── 网关 <──解析它的输出───┘

这样它就能「读你的代码 → 改 → 写回你的磁盘」，不需要它有本地工具。

## 安全边界（🔴 必读）

- 所有路径都被**限制在 workspace_root 之内**（防目录穿越）
- 默认 **只读**；写操作必须显式开 `allow_write=True`
- 拒绝符号链接逃逸、拒绝绝对路径外溢
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import config


class WorkspaceError(RuntimeError):
    pass


def _root() -> Path:
    r = Path(config.WORKSPACE_ROOT).resolve()
    r.mkdir(parents=True, exist_ok=True)
    return r


@dataclass
class FileBlob:
    path: str
    content: str
    size: int
    truncated: bool = False


def _safe(rel: str) -> Path:
    """
    把相对路径解析成 workspace 内的绝对路径。

    🔴 防目录穿越: 解析后必须仍在 root 之下，否则拒绝。
       （`..` / 绝对路径 / 符号链接都走这一道）
    """
    root = _root()
    raw = str(rel or "").strip()
    if not raw:
        raise WorkspaceError("空路径")
    # 明确拒绝绝对路径（含盘符），避免歧义
    if raw.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", raw):
        raise WorkspaceError(f"拒绝绝对路径: {raw!r}")
    p = (root / raw).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise WorkspaceError(f"路径越界（不在工作区内）: {raw!r}")
    return p


def read_file(rel: str, max_bytes: int | None = None) -> FileBlob:
    p = _safe(rel)
    if not p.exists() or not p.is_file():
        raise WorkspaceError(f"文件不存在: {rel}")
    cap = max_bytes or config.WORKSPACE_MAX_READ
    raw = p.read_bytes()
    trunc = len(raw) > cap
    txt = raw[:cap].decode("utf-8", "replace")
    return FileBlob(path=str(p.relative_to(_root())).replace("\\", "/"),
                    content=txt, size=len(raw), truncated=trunc)


def write_file(rel: str, content: str, allow_write: bool = False) -> dict:
    if not allow_write:
        raise WorkspaceError("写操作未启用（需要 allow_write=True，或在请求里带 write=true）")
    p = _safe(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"path": str(p.relative_to(_root())).replace("\\", "/"),
            "bytes": len(content.encode("utf-8"))}


def list_files(sub: str = "", limit: int = 200) -> list[dict]:
    p = _safe(sub) if sub else _root()
    if not p.exists():
        raise WorkspaceError(f"目录不存在: {sub}")
    out = []
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
    for dirpath, dirnames, filenames in os.walk(p):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            fp = Path(dirpath) / fn
            try:
                st = fp.stat()
            except OSError:
                continue
            out.append({"path": str(fp.relative_to(_root())).replace("\\", "/"),
                        "size": st.st_size})
            if len(out) >= limit:
                return out
    return out


# ---- 把 agent 的输出解析成「文件写入」指令 ----

# 约定：让 agent 用这种格式输出，网关据此写回本地
#   <<<FILE: 相对路径>>>
#   ...内容...
#   <<<END>>>
_FILE_RE = re.compile(r"<<<FILE:\s*(.+?)\s*>>>\r?\n(.*?)<<<END>>>", re.S)


def parse_file_blocks(text: str) -> list[tuple[str, str]]:
    """从 agent 回复里抽出 (路径, 内容) 列表。"""
    return [(m.group(1).strip(), m.group(2)) for m in _FILE_RE.finditer(text or "")]


def apply_file_blocks(text: str, allow_write: bool = False) -> list[dict]:
    """把回复里的文件块写回本地工作区。"""
    results = []
    for rel, content in parse_file_blocks(text):
        try:
            r = write_file(rel, content, allow_write=allow_write)
            results.append({"ok": True, **r})
        except Exception as e:
            results.append({"ok": False, "path": rel,
                            "error": f"{type(e).__name__}: {e}"})
    return results


# ---- 构造带上下文的 prompt ----

WRITE_PROTOCOL = """
【重要：文件输出约定】
你的运行环境与用户本机隔离 —— 你**看不到也改不了**用户的磁盘。
如果需要创建或修改用户的文件，请**严格按下面的格式输出**，
网关会自动把这些块写回用户本地：

<<<FILE: 相对路径/文件名>>>
（完整文件内容）
<<<END>>>

需要改多个文件就输出多个块。不要用 markdown 代码块包裹这些块，
也不要加额外说明文字在块内。不需要写文件时，正常回答即可。
""".strip()


def build_prompt(user_prompt: str, *, files: list[str] | None = None,
                 root_hint: str = "", include_tree: bool = False,
                 max_total: int | None = None) -> str:
    """
    把「本地文件内容」拼进 prompt。

    files:          要读进来的文件（相对 workspace 的路径）
    include_tree:   是否附带工作区文件树
    """
    parts = []
    budget = max_total or config.WORKSPACE_MAX_TOTAL

    if include_tree:
        try:
            tree = list_files("", limit=150)
            lines = "\n".join("  " + t["path"] for t in tree)
            parts.append(f"【工作区文件树】\n{root_hint or ''}\n{lines}")
        except Exception:
            pass

    if files:
        used = 0
        chunks = []
        for rel in files:
            try:
                fb = read_file(rel)
            except Exception as e:
                chunks.append(f"<<{rel}: 读取失败 {e}>>")
                continue
            if used + len(fb.content) > budget:
                chunks.append(f"<<{rel}: 跳过（超出上下文预算）>>")
                continue
            used += len(fb.content)
            note = "（已截断）" if fb.truncated else ""
            chunks.append(f"=== 文件: {fb.path} {note} ===\n{fb.content}\n=== 文件结束 ===")
        if chunks:
            parts.append("【用户本地文件内容】\n" + "\n\n".join(chunks))

    parts.append("【用户请求】\n" + user_prompt)
    parts.append(WRITE_PROTOCOL)
    return "\n\n".join(parts)
