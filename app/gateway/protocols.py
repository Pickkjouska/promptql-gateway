# -*- coding: utf-8 -*-
"""
protocols.py —— 三种下游协议适配

  /v1/chat/completions   OpenAI Chat Completions
  /v1/responses          OpenAI Responses API
  /v1/messages           Anthropic Messages API

统一入口: 把各家请求体归一成 (messages -> prompt)，调 pool.call_any，
再把 CallResult 渲染回各家的响应形状（含 SSE 流）。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Iterable

from .. import config

# 只做「名字归一化」。真正的 llmConfigId 由 Session.resolve_model 按项目解析，
# 因为不同项目可见/挂载的模型不同（实测：新项目默认是 Grok 4.5，
# 而 claude-fable-5-1 在项目目录里对应的是 `claude fable 5.1`）。
ALIASES = {
    "gpt-6.1-sol": "gpt-6.1 sol",
    "gpt-6-sol": "gpt-6.1 sol",
    "claude-fable-5-1": "claude fable 5.1",
    "claude-fable-5.1": "claude fable 5.1",
    "claude-opus-5-5": "claude opus 5.5",
    "claude-opus-5.5": "claude opus 5.5",
    "claude-sonnet-5": "claude sonnet 5",
    "gpt-6-astra": "gpt-6 astra",
}


def map_model(name: str | None) -> str:
    """把下游传来的模型名规范一下；解析成 id 是 Session 的事。"""
    if not name:
        return ""
    n = name.strip().lower()
    return ALIASES.get(n, name.strip())


def flatten(messages: Iterable[dict]) -> str:
    """把 messages 数组压成一段纯文本 prompt（PromptQL 是单轮 agent 入口）。"""
    parts = []
    for m in messages or []:
        role = (m.get("role") or "user").strip()
        c = m.get("content")
        if isinstance(c, list):
            txt = "".join(
                (x.get("text") or "") if isinstance(x, dict) else str(x)
                for x in c
            )
        else:
            txt = str(c or "")
        if not txt:
            continue
        parts.append(f"[{role}]\n{txt}" if role != "user" else txt)
    return "\n\n".join(parts).strip() or "(empty)"


# ---- OpenAI Chat ----

def chat_response(text: str, model: str, usage: dict, thinking: str = "",
                  tools: list | None = None) -> dict:
    msg = {"role": "assistant", "content": text}
    if thinking:
        # DeepSeek / OpenRouter 的约定字段；同时给 Anthropic 风格别名
        msg["reasoning_content"] = thinking
        msg["thinking"] = thinking
    if tools:
        # agent 的工具轨迹（run_shell / write_file / run_program …）
        msg["agent_tools"] = tools
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": msg,
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            "prompt_tokens_details": {"cached_tokens": usage.get("cached_tokens", 0)},
        },
    }


def chat_chunks(text: str, model: str, cid: str | None = None,
                chunk_size: int = 24, thinking: str = "") -> list[str]:
    cid = cid or ("chatcmpl-" + uuid.uuid4().hex[:24])
    def _u(choices, finish=None):
        return json.dumps({
            "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
            "model": model, "choices": choices,
        }, ensure_ascii=False)
    out = [f"data: {_u([{'index':0,'delta':{'role':'assistant'},'finish_reason':None}])}\n\n"]
    for i in range(0, len(text), chunk_size):
        out.append("data: " + _u([{"index": 0, "delta": {"content": text[i:i+chunk_size]},
                                   "finish_reason": None}]) + "\n\n")
    out.append(f"data: {_u([{'index':0,'delta':{},'finish_reason':'stop'}])}\n\n")
    out.append("data: [DONE]\n\n")
    return out


# ---- OpenAI Responses ----

def responses_response(text: str, model: str, usage: dict, rid: str | None = None,
                       thinking: str = "") -> dict:
    rid = rid or ("resp_" + uuid.uuid4().hex[:24])
    out = [{
        "id": "msg_" + uuid.uuid4().hex[:24],
        "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }]
    if thinking:
        out.insert(0, {
            "id": "rs_" + uuid.uuid4().hex[:24], "type": "reasoning",
            "summary": [{"type": "summary_text", "text": thinking}],
        })
    return {
        "id": rid,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": out,
        "output_text": text,
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            "input_tokens_details": {"cached_tokens": usage.get("cached_tokens", 0)},
        },
    }


def responses_events(text: str, model: str, rid: str | None = None) -> list[str]:
    rid = rid or ("resp_" + uuid.uuid4().hex[:24])
    mid = "msg_" + uuid.uuid4().hex[:16]
    def ev(name, data):
        return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    out = [
        ev("response.created", {"type": "response.created", "response": {
            "id": rid, "object": "response", "status": "in_progress", "model": model}}),
        ev("response.output_item.added", {"type": "response.output_item.added",
           "output_index": 0, "item": {"id": mid, "type": "message", "role": "assistant",
                                       "status": "in_progress", "content": []}}),
        ev("response.content_part.added", {"type": "response.content_part.added",
           "item_id": mid, "output_index": 0, "content_index": 0,
           "part": {"type": "output_text", "text": "", "annotations": []}}),
    ]
    for i in range(0, len(text), 24):
        out.append(ev("response.output_text.delta", {
            "type": "response.output_text.delta", "item_id": mid, "output_index": 0,
            "content_index": 0, "delta": text[i:i+24]}))
    out.append(ev("response.output_text.done", {
        "type": "response.output_text.done", "item_id": mid, "output_index": 0,
        "content_index": 0, "text": text}))
    out.append(ev("response.completed", {"type": "response.completed", "response": {
        "id": rid, "object": "response", "status": "completed", "model": model,
        "output": [{"id": mid, "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}]}}))
    return out


# ---- Anthropic Messages ----

def anthropic_response(text: str, model: str, usage: dict, thinking: str = "",
                       tools: list | None = None) -> dict:
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking,
                        "signature": "promptql"})
    if tools:
        content.append({"type": "agent_tool_trace", "tools": tools})
    content.append({"type": "text", "text": text})
    return {
        "id": "msg_" + uuid.uuid4().hex[:24],
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_input_tokens": usage.get("cached_tokens", 0),
        },
    }


def anthropic_events(text: str, model: str) -> list[str]:
    mid = "msg_" + uuid.uuid4().hex[:24]
    def ev(name, data):
        return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    out = [
        ev("message_start", {"type": "message_start", "message": {
            "id": mid, "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "usage": {"input_tokens": 0, "output_tokens": 0}}}),
        ev("content_block_start", {"type": "content_block_start", "index": 0,
           "content_block": {"type": "text", "text": ""}}),
    ]
    for i in range(0, len(text), 24):
        out.append(ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                   "delta": {"type": "text_delta", "text": text[i:i+24]}}))
    out.append(ev("content_block_stop", {"type": "content_block_stop", "index": 0}))
    out.append(ev("message_delta", {"type": "message_delta",
               "delta": {"stop_reason": "end_turn", "stop_sequence": None},
               "usage": {"output_tokens": 0}}))
    out.append(ev("message_stop", {"type": "message_stop"}))
    return out


def anthropic_flatten(body: dict) -> str:
    """Anthropic 用 system + messages 两处放文本。"""
    sys_p = body.get("system")
    parts = []
    if isinstance(sys_p, list):
        sys_p = "".join((x.get("text") or "") for x in sys_p if isinstance(x, dict))
    if sys_p:
        parts.append(f"[system]\n{sys_p}")
    parts.append(flatten(body.get("messages")))
    return "\n\n".join(p for p in parts if p).strip() or "(empty)"
