#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pql.py —— PromptQL 网关命令行客户端（跑 coding 任务的入口）

用法:

    # 1) 只问问题（不带本地文件）
    python pql.py ask "用 Python 写一个 LRU 缓存"

    # 2) 把本地文件给它看，让它改，并写回磁盘
    python pql.py code src/buggy.py --task "修复浮点精度问题，改用 Decimal" --write

    # 3) 给整个目录让它分析（只读，不写）
    python pql.py code src/ --task "找出所有潜在 bug" --tree

    # 4) 指定模型 / 看思考和工具轨迹
    python pql.py code src/app.py --task "重构" --model "claude opus 5.5" --show-thinking --show-tools

环境变量:
    PQL_BASE  网关地址（默认 http://127.0.0.1:8080）
    PQL_KEY   下游 API Key（不设则读 ./key.txt）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

BASE = os.getenv("PQL_BASE", "http://127.0.0.1:8080").rstrip("/")
_HERE = Path(__file__).resolve().parent


def load_key() -> str:
    k = os.getenv("PQL_KEY", "").strip()
    if k:
        return k
    for p in (_HERE / "key.txt", _HERE.parent / "_k.txt"):
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    sys.exit("找不到 API Key。设 PQL_KEY 环境变量，或把 key 写到 client/key.txt")


def call(messages: list, *, model: str = "claude opus 5.5",
         workspace: dict | None = None, timeout: int = 900) -> dict:
    body = {"model": model, "messages": messages}
    if workspace:
        body["workspace"] = workspace
    r = requests.post(f"{BASE}/v1/chat/completions",
                      headers={"Authorization": f"Bearer {load_key()}",
                               "Content-Type": "application/json"},
                      json=body, timeout=timeout)
    if r.status_code != 200:
        sys.exit(f"HTTP {r.status_code}: {r.text[:400]}")
    return r.json()


def _collect(root: str) -> tuple[list[str], str]:
    """把路径收集成 (相对文件列表, 显示用根名)"""
    p = Path(root)
    if p.is_file():
        return [p.name if p.parent == Path(".") else str(p).replace("\\", "/")], str(p.parent)
    files = []
    for dirpath, dirnames, filenames in os.walk(p):
        dirnames[:] = [d for d in dirnames
                       if d not in {".git", "node_modules", "__pycache__", ".venv"}]
        for fn in filenames:
            files.append(str((Path(dirpath) / fn)).replace("\\", "/"))
    return files, str(p)


def main():
    ap = argparse.ArgumentParser(description="PromptQL 网关客户端")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a1 = sub.add_parser("ask", help="纯问答（不碰本地文件）")
    a1.add_argument("prompt")
    a1.add_argument("--model", default="claude opus 5.5")
    a1.add_argument("--show-thinking", action="store_true")
    a1.add_argument("--show-tools", action="store_true")

    a2 = sub.add_parser("code", help="让 agent 读写本地文件")
    a2.add_argument("path", help="文件或目录（相对 WORKSPACE_ROOT）")
    a2.add_argument("--task", required=True, help="要它做什么")
    a2.add_argument("--model", default="claude opus 5.5")
    a2.add_argument("--write", action="store_true", help="允许写回磁盘（默认只读）")
    a2.add_argument("--tree", action="store_true", help="附带工作区文件树")
    a2.add_argument("--show-thinking", action="store_true")
    a2.add_argument("--show-tools", action="store_true")

    args = ap.parse_args()

    if args.cmd == "ask":
        j = call([{"role": "user", "content": args.prompt}], model=args.model)
    else:
        files, _ = _collect(args.path)
        if not files:
            sys.exit(f"路径下没有文件: {args.path}")
        print(f"[读入 {len(files)} 个文件] {', '.join(files[:5])}"
              + (" …" if len(files) > 5 else ""))
        ws = {"files": files, "write": bool(args.write),
              "include_tree": bool(args.tree), "force": bool(args.write)}
        j = call([{"role": "user", "content": args.task}],
                 model=args.model, workspace=ws)

    msg = j["choices"][0]["message"]
    print("\n" + "=" * 68)
    print(msg["content"])
    print("=" * 68)

    if args.show_thinking and msg.get("reasoning_content"):
        print("\n[思考]\n" + msg["reasoning_content"][:1500])
    if args.show_tools and msg.get("agent_tools"):
        print(f"\n[工具轨迹 {len(msg['agent_tools'])} 条]")
        for t in msg["agent_tools"][:12]:
            print("  -", str(t)[:160])
    if j.get("workspace_applied"):
        print("\n[写回结果]")
        for a in j["workspace_applied"]:
            print("  ", json.dumps(a, ensure_ascii=False))
    u = j.get("usage") or {}
    print(f"\n[用量] in={u.get('prompt_tokens')} out={u.get('completion_tokens')} "
          f"cached={(u.get('prompt_tokens_details') or {}).get('cached_tokens')}")


if __name__ == "__main__":
    main()
