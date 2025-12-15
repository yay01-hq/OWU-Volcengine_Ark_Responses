"""
title: 火山方舟 Responses API Pipeline (Fix Tool Hang + Clear Status + Usage via Retrieve)
id: volcengine_ark_responses_cache_tools_usage_fixed
required_open_webui_version: 0.4.0
description: Fixes tool-call stuck status, adds tool timeout, retrieves usage via GET /responses/{id} (no stream_options), emits ChatCompletions SSE usage chunk for OpenWebUI.
version: 1.0.0
license: MIT
requirements: httpx>=0.24.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx
from fastapi import Request
from pydantic import BaseModel, Field


# -------------------------
# Small utilities
# -------------------------
def _safe_int(x: Any, default: int = 0) -> int:
    try:
        if x is None:
            return default
        return int(x)
    except Exception:
        return default


def _stable_hash(obj: Any) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    except Exception:
        s = str(obj)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _extract_system_text(messages: List[Dict[str, Any]]) -> str:
    for m in messages or []:
        if m.get("role") == "system":
            c = m.get("content", "")
            if isinstance(c, str):
                return c
            try:
                return json.dumps(c, ensure_ascii=False)
            except Exception:
                return str(c)
    return ""


def _to_plain_dict(obj: Any) -> Optional[dict]:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass
    try:
        return obj.__dict__
    except Exception:
        return None


def _merge_numeric(dst: dict, src: dict) -> None:
    for k, v in (src or {}).items():
        if isinstance(v, (int, float)):
            dst[k] = (dst.get(k, 0) or 0) + v
        elif isinstance(v, dict):
            if k not in dst or not isinstance(dst.get(k), dict):
                dst[k] = {}
            _merge_numeric(dst[k], v)
        else:
            if k not in dst:
                dst[k] = v


# -------------------------
# Usage accumulator (multi-round)
# -------------------------
class _UsageAccumulator:
    def __init__(self) -> None:
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.prompt_tokens_details: dict = {}
        self.completion_tokens_details: dict = {}

    def add(self, usage_any: Any) -> None:
        usage = _to_plain_dict(usage_any) or {}
        if not isinstance(usage, dict) or not usage:
            return

        prompt = usage.get("input_tokens", usage.get("prompt_tokens"))
        completion = usage.get("output_tokens", usage.get("completion_tokens"))

        self.prompt_tokens += _safe_int(prompt, 0)
        self.completion_tokens += _safe_int(completion, 0)

        p_det = usage.get("input_tokens_details", usage.get("prompt_tokens_details"))
        c_det = usage.get(
            "output_tokens_details", usage.get("completion_tokens_details")
        )
        if isinstance(p_det, dict):
            _merge_numeric(self.prompt_tokens_details, p_det)
        if isinstance(c_det, dict):
            _merge_numeric(self.completion_tokens_details, c_det)

    def to_openai_chat_usage(self) -> dict:
        out = {
            "prompt_tokens": int(self.prompt_tokens),
            "completion_tokens": int(self.completion_tokens),
            "total_tokens": int(self.prompt_tokens + self.completion_tokens),
        }
        if self.prompt_tokens_details:
            out["prompt_tokens_details"] = self.prompt_tokens_details
        if self.completion_tokens_details:
            out["completion_tokens_details"] = self.completion_tokens_details
        return out


# -------------------------
# ChatCompletions SSE for OpenWebUI usage panel
# -------------------------
class _ChatCompletionSSE:
    def __init__(self, model: str) -> None:
        self.stream_id = f"chatcmpl-{uuid.uuid4().hex}"
        self.created = int(datetime.now().timestamp())
        self.model = model

    def _base(self) -> dict:
        return {
            "id": self.stream_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
        }

    def role_chunk(self, role: str = "assistant") -> bytes:
        payload = self._base()
        payload["choices"] = [
            {"index": 0, "delta": {"role": role}, "finish_reason": None}
        ]
        return self._line(payload)

    def content_chunk(self, content: str) -> bytes:
        payload = self._base()
        payload["choices"] = [
            {"index": 0, "delta": {"content": content}, "finish_reason": None}
        ]
        return self._line(payload)

    def finish_chunk(self, reason: str = "stop") -> bytes:
        payload = self._base()
        payload["choices"] = [{"index": 0, "delta": {}, "finish_reason": reason}]
        return self._line(payload)

    def usage_chunk(self, usage: dict) -> bytes:
        payload = self._base()
        payload["choices"] = []
        payload["usage"] = usage
        return self._line(payload)

    def done_line(self) -> bytes:
        return b"data: [DONE]\n\n"

    @staticmethod
    def _line(payload: dict) -> bytes:
        # 关键：补齐 SSE 分隔符 \n\n，避免“最后一块 usage 有时不被前端消费”
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


# -------------------------
# Message transform (keep system in input, no instructions)
# -------------------------
def _parse_content_items(role: str, raw_content: Any) -> Any:
    # Keep pure string as-is
    if isinstance(raw_content, str):
        return raw_content

    content_list = raw_content if isinstance(raw_content, list) else [raw_content]
    parsed_items: List[Dict[str, Any]] = []

    for item in content_list:
        if isinstance(item, str):
            parsed_items.append(
                {
                    "type": "output_text" if role == "assistant" else "input_text",
                    "text": item,
                }
            )
            continue

        if not isinstance(item, dict):
            parsed_items.append(
                {
                    "type": "output_text" if role == "assistant" else "input_text",
                    "text": str(item),
                }
            )
            continue

        item_type = item.get("type", "text")

        if item_type == "text":
            parsed_items.append(
                {
                    "type": "output_text" if role == "assistant" else "input_text",
                    "text": item.get("text", ""),
                }
            )
            continue

        if item_type == "image_url":
            url_data = item.get("image_url", {})
            url = (
                url_data.get("url", "") if isinstance(url_data, dict) else str(url_data)
            )
            parsed_items.append({"type": "input_image", "image_url": url})
            continue

        if item_type in {
            "input_text",
            "input_image",
            "input_video",
            "input_file",
            "output_text",
            "refusal",
        }:
            parsed_items.append(item)
            continue

        parsed_items.append(
            {
                "type": "output_text" if role == "assistant" else "input_text",
                "text": json.dumps(item, ensure_ascii=False),
            }
        )

    return parsed_items


def _transform_messages_keep_system(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for m in messages or []:
        role = m.get("role", "user")
        out.append(
            {"role": role, "content": _parse_content_items(role, m.get("content", ""))}
        )
    return out


def _last_user_message_as_input(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    last_user = None
    for m in reversed(messages or []):
        if m.get("role") == "user":
            last_user = m
            break
    if not last_user:
        return []
    return [
        {
            "role": "user",
            "content": _parse_content_items("user", last_user.get("content", "")),
        }
    ]


# -------------------------
# Usage retrieve: GET /responses/{id}
# -------------------------
async def _retrieve_response_json(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict,
    response_id: str,
) -> Optional[dict]:
    try:
        url = base_url.rstrip("/") + f"/responses/{response_id}"
        r = await client.get(url, headers=headers)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _extract_usage_from_response_obj(resp_obj: Any) -> Optional[dict]:
    d = _to_plain_dict(resp_obj)
    if not isinstance(d, dict) or not d:
        return None
    if isinstance(d.get("usage"), dict):
        return d.get("usage")
    if isinstance(d.get("response"), dict) and isinstance(
        d["response"].get("usage"), dict
    ):
        return d["response"]["usage"]
    return None


async def _fetch_usage_with_retry(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict,
    response_id: str,
    retries: int,
    delay_ms: int,
) -> Optional[dict]:
    for i in range(max(1, retries)):
        resp_json = await _retrieve_response_json(
            client, base_url, headers, response_id
        )
        usage_obj = _extract_usage_from_response_obj(resp_json)
        if usage_obj:
            return usage_obj
        if i < retries - 1:
            await asyncio.sleep((delay_ms / 1000.0) * (i + 1))
    return None


# -------------------------
# Tool execution with timeout + clear status
# -------------------------
async def _run_tool_callable(
    callable_obj: Callable[..., Any], args: dict, timeout_s: int
) -> Any:
    async def _invoke():
        if asyncio.iscoroutinefunction(callable_obj):
            return await callable_obj(**args)
        return await asyncio.to_thread(callable_obj, **args)

    return await asyncio.wait_for(_invoke(), timeout=timeout_s)


@dataclass
class _SessionState:
    previous_response_id: Optional[str] = None
    tool_fingerprint: str = ""
    system_fingerprint: str = ""
    last_messages_len: int = 0
    tools_attached: bool = False


class Pipe:
    class Valves(BaseModel):
        BASE_URL: str = Field(
            default="https://ark.cn-beijing.volces.com/api/v3",
            description="火山方舟 API 基础 URL",
        )
        API_KEY: str = Field(
            default="",
            description="火山方舟 API Key（留空则读环境变量 VOLCENGINE_API_KEY）",
        )
        MODEL_ID: str = Field(default="kimi-k2", description="模型 ID")

        TEMPERATURE: float = Field(default=1.0, description="temperature")
        TOP_P: float = Field(default=0.7, description="top_p [0,1]")
        MAX_OUTPUT_TOKENS: Optional[int] = Field(
            default=None, description="max_output_tokens"
        )

        THINKING_TYPE: Optional[str] = Field(
            default=None, description="thinking.type: enabled|disabled|auto；留空不传"
        )
        REASONING_EFFORT: Optional[str] = Field(
            default=None,
            description="reasoning.effort: minimal|low|medium|high；留空不传",
        )

        ENABLE_WEB_SEARCH: bool = Field(
            default=False, description="启用 web_search 工具（若模型支持）"
        )
        WEB_SEARCH_LIMIT: int = Field(default=10, description="web_search limit [1,50]")

        STORE: bool = Field(
            default=True, description="store=true 才能用 previous_response_id"
        )
        ENABLE_CACHING: bool = Field(default=True, description="caching: enabled")
        ENABLE_SESSION_CACHE: bool = Field(
            default=True, description="使用 previous_response_id 续写"
        )

        MAX_TOOL_ROUNDS: int = Field(default=15, description="最多工具轮次")
        TOOL_TIMEOUT_SECONDS: int = Field(
            default=90, description="单次工具调用超时(秒)"
        )

        FORCE_CHAT_COMPLETIONS_SSE: bool = Field(
            default=False,
            description="即使 include_usage=false 也强制用 ChatCompletions SSE 输出",
        )
        FORWARD_STREAM_OPTIONS: bool = Field(
            default=False,
            description="是否转发 stream_options 到火山方舟（/responses 通常不支持）",
        )

        USAGE_RETRIEVE_RETRIES: int = Field(default=3, description="用量查询重试次数")
        USAGE_RETRIEVE_DELAY_MS: int = Field(
            default=200, description="用量查询重试间隔(ms)"
        )

        DEBUG: bool = Field(default=False, description="调试日志")
        TIMEOUT: int = Field(default=600, description="请求超时(秒)")

    def __init__(self) -> None:
        self.valves = self.Valves()
        self.name = f"火山方舟: {self.valves.MODEL_ID}"
        self._sessions: Dict[str, _SessionState] = {}
        self._lock = asyncio.Lock()

    async def on_valves_updated(self):
        self.name = f"火山方舟: {self.valves.MODEL_ID}"

    def _conversation_key(
        self,
        body: Dict[str, Any],
        __metadata__: Dict[str, Any],
        __user__: Dict[str, Any],
    ) -> str:
        user_id = str(
            (__user__ or {}).get("id")
            or (__user__ or {}).get("user_id")
            or (__user__ or {}).get("email")
            or "anon"
        )
        chat_id = (
            (__metadata__ or {}).get("chat_id")
            or (__metadata__ or {}).get("conversation_id")
            or (__metadata__ or {}).get("id")
            or (body or {}).get("chat_id")
            or (body or {}).get("conversation_id")
            or (body or {}).get("id")
        )
        if not chat_id:
            chat_id = _stable_hash(((body or {}).get("messages", []) or [])[:2])
        return f"{user_id}:{chat_id}:{self.valves.MODEL_ID}"

    async def _emit_status(
        self, __event_emitter__, description: str, done: bool
    ) -> None:
        if not __event_emitter__:
            return
        try:
            await __event_emitter__(
                {"type": "status", "data": {"description": description, "done": done}}
            )
        except Exception:
            # 状态事件失败不应影响主流程
            return

    async def pipe(
        self,
        body: Dict[str, Any],
        __user__: Dict[str, Any],
        __request__: Request,
        __event_emitter__: Callable[[Any], Awaitable[None]],
        __event_call__: Callable[[Dict[str, Any]], Awaitable[Any]],
        __task__: str,
        __task_body__: Dict[str, Any],
        __files__: List[Dict[str, Any]],
        __metadata__: Dict[str, Any],
        __tools__: Dict[str, Any],
    ):
        api_key = self.valves.API_KEY or os.environ.get("VOLCENGINE_API_KEY", "")
        if not api_key:
            yield "❌ 错误: 未配置 API Key（Valves.API_KEY 或环境变量 VOLCENGINE_API_KEY）"
            return

        messages = (body or {}).get("messages", []) or []

        # OpenWebUI 用量开关：stream_options.include_usage
        stream_options = (body or {}).get("stream_options") or {}
        include_usage = bool(stream_options.get("include_usage", False))

        emit_sse = bool(include_usage or self.valves.FORCE_CHAT_COMPLETIONS_SSE)
        sse = _ChatCompletionSSE(self.valves.MODEL_ID) if emit_sse else None
        usage_acc = _UsageAccumulator()

        # Tools list (OpenWebUI tools specs + optional web_search)
        tools: List[Dict[str, Any]] = []
        if __tools__:
            for _, tool_info in (__tools__ or {}).items():
                spec = (tool_info or {}).get("spec", {}) or {}
                if isinstance(spec, dict):
                    if "type" not in spec:
                        spec["type"] = "function"
                    tools.append(spec)

        if self.valves.ENABLE_WEB_SEARCH:
            tools.append(
                {"type": "web_search", "limit": int(self.valves.WEB_SEARCH_LIMIT)}
            )

        tool_fp = _stable_hash(tools)
        sys_fp = _stable_hash(_extract_system_text(messages))
        conv_key = self._conversation_key(
            body or {}, __metadata__ or {}, __user__ or {}
        )

        async with self._lock:
            st = self._sessions.get(conv_key)
            if st is None:
                st = _SessionState()
                self._sessions[conv_key] = st

            # 用户回滚/删除历史
            if len(messages) < st.last_messages_len:
                st.previous_response_id = None
                st.tools_attached = False

            # system 或 tools 变化则重置会话
            if sys_fp and sys_fp != st.system_fingerprint:
                st.previous_response_id = None
                st.tools_attached = False
            if tool_fp and tool_fp != st.tool_fingerprint:
                st.previous_response_id = None
                st.tools_attached = False

            st.system_fingerprint = sys_fp
            st.tool_fingerprint = tool_fp
            st.last_messages_len = len(messages)

            session_prev_id = st.previous_response_id
            tools_attached = st.tools_attached

        use_session = bool(
            self.valves.ENABLE_SESSION_CACHE and self.valves.STORE and session_prev_id
        )

        # 第一次 round 输入
        if use_session:
            round_input = _last_user_message_as_input(messages)
        else:
            round_input = _transform_messages_keep_system(messages)

        # 先发 role chunk
        if emit_sse and sse is not None:
            yield sse.role_chunk("assistant")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        base_url = self.valves.BASE_URL.rstrip("/")
        create_url = f"{base_url}/responses"

        in_think = False
        output_started = False
        think_buf: List[str] = []

        final_response_id: Optional[str] = None
        prev_id_for_next_call: Optional[str] = session_prev_id if use_session else None

        tool_round = 0

        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as client:
            try:
                while True:
                    request_params: Dict[str, Any] = {
                        "model": self.valves.MODEL_ID,
                        "input": round_input,
                        "stream": True,
                    }

                    if self.valves.STORE:
                        request_params["store"] = True
                    if self.valves.ENABLE_CACHING:
                        request_params["caching"] = {"type": "enabled"}
                    if prev_id_for_next_call:
                        request_params["previous_response_id"] = prev_id_for_next_call

                    # /responses 通常不支持 stream_options；默认不转发
                    if (
                        self.valves.FORWARD_STREAM_OPTIONS
                        and isinstance(stream_options, dict)
                        and stream_options
                    ):
                        request_params["stream_options"] = stream_options

                    # generation params
                    temp = (body or {}).get("temperature")
                    if temp is not None:
                        request_params["temperature"] = temp
                    else:
                        request_params["temperature"] = self.valves.TEMPERATURE

                    top_p = (body or {}).get("top_p")
                    if top_p is not None:
                        request_params["top_p"] = top_p
                    else:
                        request_params["top_p"] = self.valves.TOP_P

                    max_tokens = (body or {}).get(
                        "max_tokens"
                    ) or self.valves.MAX_OUTPUT_TOKENS
                    if max_tokens:
                        request_params["max_output_tokens"] = max_tokens

                    if self.valves.THINKING_TYPE:
                        request_params["thinking"] = {"type": self.valves.THINKING_TYPE}
                    if self.valves.REASONING_EFFORT:
                        request_params["reasoning"] = {
                            "effort": self.valves.REASONING_EFFORT
                        }

                    # 关键：带 previous_response_id 时不要再 set tools（否则可能 400）
                    if tools and (not prev_id_for_next_call) and (not tools_attached):
                        request_params["tools"] = tools

                    if self.valves.DEBUG:
                        safe_params = dict(request_params)
                        safe_params.pop("input", None)
                        print(
                            "[DEBUG] request_params:",
                            json.dumps(safe_params, ensure_ascii=False),
                        )

                    pending_function_calls: List[Dict[str, Any]] = []
                    response_id_for_round: Optional[str] = None
                    round_usage_added = False

                    # 发起请求（最多一次自动修正：移除 stream_options / tools / caching）
                    for attempt in range(2):
                        async with client.stream(
                            "POST", create_url, headers=headers, json=request_params
                        ) as resp:
                            if resp.status_code != 200:
                                err_text = (await resp.aread()).decode(
                                    "utf-8", errors="ignore"
                                )

                                if (
                                    attempt == 0
                                    and resp.status_code == 400
                                    and 'unknown field "stream_options"' in err_text
                                    and "stream_options" in request_params
                                ):
                                    request_params.pop("stream_options", None)
                                    continue

                                if (
                                    attempt == 0
                                    and resp.status_code == 400
                                    and "previous response cached" in err_text
                                    and "tools" in request_params
                                ):
                                    request_params.pop("tools", None)
                                    continue

                                if (
                                    attempt == 0
                                    and resp.status_code == 400
                                    and ("caching" in err_text or "cache" in err_text)
                                    and "caching" in request_params
                                ):
                                    request_params.pop("caching", None)
                                    continue

                                msg = f"❌ API 错误({resp.status_code}): {err_text}"
                                if emit_sse and sse is not None:
                                    yield sse.content_chunk(msg)
                                else:
                                    yield msg
                                return

                            async for line in resp.aiter_lines():
                                if not line or not line.startswith("data:"):
                                    continue
                                data_str = line[5:].strip()

                                if data_str == "[DONE]":
                                    break

                                try:
                                    event = json.loads(data_str)
                                except Exception:
                                    continue

                                event_type = event.get("type", "")

                                # response_id
                                if isinstance(event.get("response"), dict) and event[
                                    "response"
                                ].get("id"):
                                    response_id_for_round = event["response"]["id"]
                                elif isinstance(event.get("response_id"), str):
                                    response_id_for_round = event.get("response_id")
                                elif isinstance(event.get("id"), str) and event[
                                    "id"
                                ].startswith("resp_"):
                                    response_id_for_round = event["id"]

                                # usage from stream (if any)
                                usage_any = None
                                if isinstance(event.get("usage"), dict):
                                    usage_any = event.get("usage")
                                elif isinstance(
                                    event.get("response"), dict
                                ) and isinstance(event["response"].get("usage"), dict):
                                    usage_any = event["response"].get("usage")
                                if usage_any and (not round_usage_added):
                                    usage_acc.add(usage_any)
                                    round_usage_added = True

                                # tool status (start)
                                if event_type == "response.output_item.added":
                                    item = event.get("item", {}) or {}
                                    if item.get("type") == "function_call":
                                        await self._emit_status(
                                            __event_emitter__,
                                            f"调用工具: {item.get('name','')}",
                                            False,
                                        )

                                # tool call item
                                if event_type == "response.output_item.done":
                                    item = event.get("item", {}) or {}
                                    if item.get("type") == "function_call":
                                        pending_function_calls.append(item)

                                # reasoning
                                if (
                                    event_type
                                    == "response.reasoning_summary_text.delta"
                                ):
                                    delta = event.get("delta", "")
                                    if delta:
                                        if (not in_think) and (not output_started):
                                            if emit_sse and sse is not None:
                                                yield sse.content_chunk("<think>")
                                            else:
                                                yield "<think>"
                                            in_think = True

                                        if in_think and (not output_started):
                                            think_buf.append(delta)
                                            if emit_sse and sse is not None:
                                                yield sse.content_chunk(delta)
                                            else:
                                                yield delta

                                elif (
                                    event_type == "response.reasoning_summary_text.done"
                                ):
                                    if in_think and (not output_started):
                                        if emit_sse and sse is not None:
                                            yield sse.content_chunk("\n")
                                        else:
                                            yield "\n"

                                # output
                                elif event_type == "response.output_text.delta":
                                    delta = event.get("delta", "")
                                    if delta:
                                        if in_think and (not output_started):
                                            if emit_sse and sse is not None:
                                                yield sse.content_chunk("</think>\n")
                                            else:
                                                yield "</think>\n"
                                            in_think = False
                                            output_started = True

                                        if emit_sse and sse is not None:
                                            yield sse.content_chunk(delta)
                                        else:
                                            yield delta

                            break  # 成功请求后不再重试

                    # 更新 response_id
                    if response_id_for_round:
                        final_response_id = response_id_for_round
                        prev_id_for_next_call = response_id_for_round

                        # tools attached once
                        if tools and ("tools" in request_params):
                            tools_attached = True
                            async with self._lock:
                                st2 = self._sessions.get(conv_key)
                                if st2:
                                    st2.tools_attached = True

                        # 如果流里没给 usage，用 GET /responses/{id} 补
                        if include_usage and (not round_usage_added):
                            usage_obj = await _fetch_usage_with_retry(
                                client=client,
                                base_url=base_url,
                                headers=headers,
                                response_id=response_id_for_round,
                                retries=int(self.valves.USAGE_RETRIEVE_RETRIES),
                                delay_ms=int(self.valves.USAGE_RETRIEVE_DELAY_MS),
                            )
                            if usage_obj:
                                usage_acc.add(usage_obj)
                                round_usage_added = True

                    # 需要工具调用
                    if pending_function_calls:
                        tool_round += 1
                        if tool_round > int(self.valves.MAX_TOOL_ROUNDS):
                            warn = "\n⚠️工具调用轮次达到上限，已停止继续调用。\n"
                            if emit_sse and sse is not None:
                                yield sse.content_chunk(warn)
                            else:
                                yield warn
                            await self._emit_status(__event_emitter__, "", True)
                            break

                        tool_tasks: List[asyncio.Task] = []
                        metas: List[Tuple[str, str]] = []
                        tool_outputs: List[Dict[str, Any]] = []

                        # 对每个 function_call 都给出一个 output（即使 tool 不存在）
                        for fc in pending_function_calls:
                            name = fc.get("name")
                            call_id = fc.get("call_id")
                            if not name or not call_id:
                                continue

                            tool = (__tools__ or {}).get(name)
                            args = {}
                            try:
                                args = json.loads(fc.get("arguments") or "{}")
                                if not isinstance(args, dict):
                                    args = {}
                            except Exception:
                                args = {}

                            if not tool or "callable" not in tool:
                                tool_outputs.append(
                                    {
                                        "type": "function_call_output",
                                        "call_id": str(call_id),
                                        "output": f"Tool not found: {name}",
                                    }
                                )
                                continue

                            metas.append((str(call_id), str(name)))
                            callable_obj = tool["callable"]
                            tool_tasks.append(
                                asyncio.create_task(
                                    _run_tool_callable(
                                        callable_obj,
                                        args,
                                        int(self.valves.TOOL_TIMEOUT_SECONDS),
                                    )
                                )
                            )

                        results: List[Any] = []
                        if tool_tasks:
                            results = await asyncio.gather(
                                *tool_tasks, return_exceptions=True
                            )

                        for (call_id, name), r in zip(metas, results):
                            if isinstance(r, asyncio.TimeoutError):
                                out_str = f"Timeout: {name} exceeded {self.valves.TOOL_TIMEOUT_SECONDS}s"
                            elif isinstance(r, Exception):
                                out_str = f"Error: {type(r).__name__}: {r}"
                            else:
                                if isinstance(r, (dict, list)):
                                    try:
                                        out_str = json.dumps(r, ensure_ascii=False)
                                    except Exception:
                                        out_str = str(r)
                                else:
                                    out_str = str(r)

                            tool_outputs.append(
                                {
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": out_str,
                                }
                            )

                            # citation 不要让异常打断主流程
                            if __event_emitter__:
                                try:
                                    await __event_emitter__(
                                        {
                                            "type": "citation",
                                            "data": {
                                                "document": [
                                                    f"{name} 输出\n\n{out_str}"
                                                ],
                                                "metadata": [{"source": name}],
                                                "source": {"name": f"{name} 工具"},
                                            },
                                        }
                                    )
                                except Exception:
                                    pass

                        # 关键：清掉“调用工具”状态，避免 UI 一直卡着
                        await self._emit_status(__event_emitter__, "", True)

                        # 下一轮只发 tool outputs，用 previous_response_id 续写
                        round_input = tool_outputs
                        continue

                    # 无工具调用：结束
                    await self._emit_status(__event_emitter__, "", True)
                    break

            except Exception as exc:
                err_text = f"❌ {type(exc).__name__}: {exc}\n{''.join(traceback.format_exc(limit=5))}"
                if emit_sse and sse is not None:
                    yield sse.content_chunk(err_text)
                else:
                    yield err_text
            finally:
                # 收尾：如果还在 think，闭合
                if in_think and (not output_started):
                    if emit_sse and sse is not None:
                        yield sse.content_chunk("</think>\n")
                    else:
                        yield "</think>\n"
                    in_think = False

                # 保存会话 prev_id
                if final_response_id and self.valves.STORE:
                    async with self._lock:
                        st3 = self._sessions.get(conv_key)
                        if st3:
                            st3.previous_response_id = final_response_id

                # SSE 收尾：finish + usage + DONE
                if emit_sse and sse is not None:
                    yield sse.finish_chunk("stop")
                    if include_usage:
                        yield sse.usage_chunk(usage_acc.to_openai_chat_usage())
                    yield sse.done_line()
