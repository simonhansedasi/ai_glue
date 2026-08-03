"""
Reverse proxy routes for OpenAI and Anthropic APIs.

Point existing apps at these endpoints instead of the real providers:

    OPENAI_BASE_URL=http://<host>:5010/proxy/openai/v1
    ANTHROPIC_BASE_URL=http://<host>:5010/proxy/anthropic

Optional headers to tag calls in the audit log:
    X-Aiglue-Session: user-123
    X-Aiglue-Project: hr-bot
"""

import hashlib
import json
import os
import re
import time
from functools import lru_cache

import requests
from flask import Blueprint, Response, jsonify, request, stream_with_context

from src.governance import check, get_teams
from src.logger import log_call


bp = Blueprint("proxy", __name__)


@lru_cache(maxsize=8)
def _parse_body(body_bytes):
    """Every proxied request is independently json.loads'd by 5 helpers below
    (_detect_project, _detect_session, _get_model, _extract_prompt, _is_streaming).
    For a long Claude Code conversation the body can be megabytes, so parsing it
    5 times per turn instead of once is real, compounding latency. Cache it."""
    try:
        return json.loads(body_bytes)
    except Exception:
        return {}

OPENAI_BASE = "https://api.openai.com"
ANTHROPIC_BASE = "https://api.anthropic.com"

_STRIP = {"host", "content-length", "transfer-encoding", "connection",
          "x-aiglue-session", "x-aiglue-project"}
_STRIP_RESPONSE = {"transfer-encoding", "content-encoding", "content-length"}


def _fwd_headers(incoming):
    return {k: v for k, v in incoming.items() if k.lower() not in _STRIP}


def _match_team(key):
    """Return team name if key starts with a known prefix in governance.yaml teams section."""
    try:
        for prefix, cfg in get_teams().items():
            if key.startswith(prefix):
                return cfg.get("name", prefix)
    except Exception:
        pass
    return None


def _resolve_team(incoming_headers):
    """If the incoming API key is an aiglue team key, return (team_name, header_to_replace).
    header_to_replace is 'x-api-key' (Anthropic) or 'authorization' (OpenAI).
    Returns (None, None) if not a team key."""
    api_key = incoming_headers.get("X-Api-Key", "")
    if api_key.startswith("sk-aiglue-"):
        team = _match_team(api_key)
        if team:
            return team, "x-api-key"

    auth = incoming_headers.get("Authorization", "")
    if auth.lower().startswith("bearer sk-aiglue-"):
        team = _match_team(auth[7:])  # strip "Bearer "
        if team:
            return team, "authorization"

    return None, None


def _swap_auth(headers, header_name, new_value):
    """Replace a header case-insensitively."""
    for k in list(headers.keys()):
        if k.lower() == header_name:
            headers[k] = new_value
            return
    headers[header_name] = new_value


def _detect_project(body_bytes):
    """Infer project from tool_use file paths in the conversation.

    Scans tool_use blocks (Read, Write, Edit, Bash, etc.) in assistant messages
    for /coding/<project>/ file paths and returns the most-cited project name.
    Falls back to the working directory line in the system prompt if no tool calls
    have been made yet (first turn of a fresh conversation).
    """
    try:
        from collections import Counter
        body = _parse_body(body_bytes)

        # Primary signal: file paths in tool_use input blocks.
        # ponytail: only the last 20 messages are scanned (not the whole growing
        # history) since project never changes mid-session in practice; full
        # rescan every turn was the O(n^2) cost per conversation. Widen the
        # window if a session is ever seen switching projects mid-stream.
        counts = Counter()
        for msg in body.get("messages", [])[-20:]:
            content = msg.get("content", "")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                inp = block.get("input", {})
                for key in ("file_path", "path", "notebook_path", "command"):
                    val = inp.get(key, "") or ""
                    m = re.search(r"/coding/([^/\s]+)/", val)
                    if m:
                        counts[m.group(1)] += 1
        if counts:
            return counts.most_common(1)[0][0]

        # Fallback: working directory in system (only matches project subdirectories)
        system = body.get("system", "")
        if isinstance(system, list):
            system = " ".join(b.get("text", "") for b in system
                              if isinstance(b, dict) and b.get("type") == "text")
        m = re.search(r"Primary working directory: [^\n]*/coding/([^/\n\s]+)", system)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _detect_session(body_bytes):
    """Derive a stable session ID from the first message — same across all turns of a conversation."""
    try:
        body = _parse_body(body_bytes)
        messages = body.get("messages", [])
        if messages:
            first = messages[0].get("content", "")
            if isinstance(first, list):
                first = " ".join(b.get("text", "") for b in first
                                 if isinstance(b, dict) and b.get("type") == "text")
            if first:
                return "conv-" + hashlib.md5(first.encode()).hexdigest()[:10]
    except Exception:
        pass
    return None


def _clean_response_headers(upstream_headers):
    return {k: v for k, v in upstream_headers.items() if k.lower() not in _STRIP_RESPONSE}


def _extract_prompt(body_bytes, provider):
    """Return only the newest message's text, not the whole growing conversation.

    The Anthropic/OpenAI APIs resend full message history on every call, so joining
    every message here re-stored (and re-hashed, and re-rendered on the dashboard)
    the entire conversation-so-far on every single turn — O(n^2) work and DB bytes
    over a session's lifetime for content that's already logged in earlier rows.
    """
    try:
        body = _parse_body(body_bytes)
        messages = body.get("messages", [])
        if not messages:
            return ""
        content = messages[-1].get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " | ".join(
                block["text"] for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        return ""
    except Exception:
        return ""


def _parse_openai_stream(chunks):
    text_parts, input_tokens, output_tokens = [], 0, 0
    for chunk in chunks:
        for line in chunk.decode("utf-8", errors="ignore").splitlines():
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data.strip() == "[DONE]":
                continue
            try:
                obj = json.loads(data)
                for choice in obj.get("choices", []):
                    delta_content = choice.get("delta", {}).get("content")
                    if delta_content:
                        text_parts.append(delta_content)
                if obj.get("usage"):
                    input_tokens = obj["usage"].get("prompt_tokens", 0)
                    output_tokens = obj["usage"].get("completion_tokens", 0)
            except Exception:
                pass
    return "".join(text_parts), input_tokens, output_tokens


def _parse_openai_full(body_bytes):
    try:
        obj = json.loads(body_bytes)
        text = (obj.get("choices") or [{}])[0].get("message", {}).get("content", "")
        usage = obj.get("usage", {})
        return text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    except Exception:
        return "", 0, 0


def _trim_tool_input(inp, max_str=300):
    if not isinstance(inp, dict):
        return inp
    result = {}
    for k, v in inp.items():
        if isinstance(v, str) and len(v) > max_str:
            result[k] = v[:max_str] + f"... [{len(v)} chars]"
        else:
            result[k] = v
    return result


def _parse_anthropic_stream(chunks):
    text_parts, input_tokens, output_tokens = [], 0, 0
    tool_blocks = {}  # index -> {name, json_parts}
    tool_calls = []
    for chunk in chunks:
        for line in chunk.decode("utf-8", errors="ignore").splitlines():
            if not line.startswith("data:"):
                continue
            try:
                obj = json.loads(line[5:].strip())
                t = obj.get("type", "")
                if t == "message_start":
                    input_tokens = obj.get("message", {}).get("usage", {}).get("input_tokens", 0)
                elif t == "content_block_start":
                    block = obj.get("content_block", {})
                    if block.get("type") == "tool_use":
                        tool_blocks[obj.get("index", 0)] = {
                            "name": block.get("name", ""),
                            "json_parts": [],
                        }
                elif t == "content_block_delta":
                    idx = obj.get("index", 0)
                    delta = obj.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text_parts.append(delta.get("text", ""))
                    elif delta.get("type") == "input_json_delta" and idx in tool_blocks:
                        tool_blocks[idx]["json_parts"].append(delta.get("partial_json", ""))
                elif t == "content_block_stop":
                    idx = obj.get("index", 0)
                    if idx in tool_blocks:
                        blk = tool_blocks.pop(idx)
                        raw = "".join(blk["json_parts"])
                        try:
                            inp = _trim_tool_input(json.loads(raw)) if raw else {}
                        except Exception:
                            inp = {"_raw": raw[:200]}
                        tool_calls.append({"name": blk["name"], "input": inp})
                elif t == "message_delta":
                    output_tokens = obj.get("usage", {}).get("output_tokens", 0)
            except Exception:
                pass
    return "".join(text_parts), input_tokens, output_tokens, tool_calls


def _parse_anthropic_full(body_bytes):
    try:
        obj = json.loads(body_bytes)
        text = (obj.get("content") or [{}])[0].get("text", "")
        usage = obj.get("usage", {})
        return text, usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    except Exception:
        return "", 0, 0


def _is_streaming(body_bytes):
    try:
        return bool(_parse_body(body_bytes).get("stream", False))
    except Exception:
        return False


def _get_model(body_bytes):
    try:
        return _parse_body(body_bytes).get("model", "")
    except Exception:
        return ""


# ── OpenAI proxy ─────────────────────────────────────────────────────────────

@bp.route("/proxy/openai/<path:path>", methods=["GET", "POST", "PUT", "DELETE"])
def proxy_openai(path):
    body_bytes = request.get_data()

    team_name, team_auth_header = _resolve_team(request.headers)
    project = (request.headers.get("X-Aiglue-Project")
               or team_name
               or _detect_project(body_bytes)
               or os.getenv("AIGLUE_DEFAULT_PROJECT", "proxy"))
    session_id = (request.headers.get("X-Aiglue-Session")
                  or _detect_session(body_bytes)
                  or os.getenv("AIGLUE_DEFAULT_SESSION", "proxy"))

    model = _get_model(body_bytes)
    prompt = _extract_prompt(body_bytes, "openai")
    streaming = _is_streaming(body_bytes)

    try:
        gov_flags = check("openai", model, project, session_id, prompt)
    except ValueError as e:
        return jsonify({"error": {"message": str(e), "type": "governance_error"}}), 403

    url = f"{OPENAI_BASE}/{path}"
    headers = _fwd_headers(request.headers)
    if team_auth_header:
        _swap_auth(headers, "authorization", f"Bearer {os.getenv('OPENAI_API_KEY', '')}")
    t0 = time.time()

    try:
        upstream = requests.request(
            method=request.method,
            url=url,
            headers=headers,
            data=body_bytes,
            params=request.args,
            stream=streaming,
            timeout=120,
        )
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        log_call("openai", model, session_id, project, 0, 0, latency_ms,
                 prompt, "", gov_flags, error=str(e))
        return jsonify({"error": str(e)}), 502

    if streaming:
        chunks = []

        def generate():
            for chunk in upstream.iter_content(chunk_size=None):
                chunks.append(chunk)
                yield chunk
            latency_ms = int((time.time() - t0) * 1000)
            text, inp, out = _parse_openai_stream(chunks)
            log_call("openai", model, session_id, project, inp, out,
                     latency_ms, prompt, text, gov_flags)

        return Response(
            stream_with_context(generate()),
            status=upstream.status_code,
            headers=_clean_response_headers(upstream.headers),
        )

    latency_ms = int((time.time() - t0) * 1000)
    body = upstream.content
    text, inp, out = _parse_openai_full(body)
    error = body.decode("utf-8", errors="ignore")[:500] if upstream.status_code >= 400 else None
    log_call("openai", model, session_id, project, inp, out,
             latency_ms, prompt, text, gov_flags, error=error)

    return Response(body, status=upstream.status_code,
                    content_type=upstream.headers.get("content-type", "application/json"))


# ── Anthropic proxy ───────────────────────────────────────────────────────────

@bp.route("/proxy/anthropic/<path:path>", methods=["GET", "POST", "PUT", "DELETE"])
def proxy_anthropic(path):
    body_bytes = request.get_data()

    team_name, team_auth_header = _resolve_team(request.headers)
    project = (request.headers.get("X-Aiglue-Project")
               or team_name
               or _detect_project(body_bytes)
               or os.getenv("AIGLUE_DEFAULT_PROJECT", "proxy"))
    session_id = (request.headers.get("X-Aiglue-Session")
                  or _detect_session(body_bytes)
                  or os.getenv("AIGLUE_DEFAULT_SESSION", "proxy"))

    model = _get_model(body_bytes)
    prompt = _extract_prompt(body_bytes, "anthropic")
    streaming = _is_streaming(body_bytes)

    try:
        gov_flags = check("anthropic", model, project, session_id, prompt)
    except ValueError as e:
        return jsonify({"error": {"type": "governance_error", "message": str(e)}}), 403

    url = f"{ANTHROPIC_BASE}/{path}"
    headers = _fwd_headers(request.headers)
    if team_auth_header:
        _swap_auth(headers, "x-api-key", os.getenv("ANTHROPIC_API_KEY", ""))
    t0 = time.time()

    try:
        upstream = requests.request(
            method=request.method,
            url=url,
            headers=headers,
            data=body_bytes,
            params=request.args,
            stream=streaming,
            timeout=120,
        )
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        log_call("anthropic", model, session_id, project, 0, 0, latency_ms,
                 prompt, "", gov_flags, error=str(e))
        return jsonify({"error": str(e)}), 502

    if streaming:
        chunks = []

        def generate():
            for chunk in upstream.iter_content(chunk_size=None):
                chunks.append(chunk)
                yield chunk
            latency_ms = int((time.time() - t0) * 1000)
            text, inp, out, tool_calls = _parse_anthropic_stream(chunks)
            log_call("anthropic", model, session_id, project, inp, out,
                     latency_ms, prompt, text, gov_flags, tool_calls=tool_calls)

        return Response(
            stream_with_context(generate()),
            status=upstream.status_code,
            headers=_clean_response_headers(upstream.headers),
        )

    latency_ms = int((time.time() - t0) * 1000)
    body = upstream.content
    text, inp, out = _parse_anthropic_full(body)
    error = body.decode("utf-8", errors="ignore")[:500] if upstream.status_code >= 400 else None
    log_call("anthropic", model, session_id, project, inp, out,
             latency_ms, prompt, text, gov_flags, error=error)

    return Response(body, status=upstream.status_code,
                    content_type=upstream.headers.get("content-type", "application/json"))
