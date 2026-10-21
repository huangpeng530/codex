#!/usr/bin/env python3
"""Agnes codex Responses-API proxy (namespace flattening + gateway compat).

- Flattens `type:"namespace"` tools into dotted-name function tools.
- Strips `tool_choice` when no tools are present (hub rejects it).
- Image bridge: the hub's /v1/responses translator drops image content
  parts (`input_image`/`input_file` -> `type:"unknown"` -> HTTP 400).
  Requests that carry image parts are rewritten to /v1/chat/completions
  (which fully supports images) and the answer is re-wrapped into
  Responses-API JSON/SSE. Text-only requests still pass through
  /v1/responses untouched.
- Stdlib only; Pillow is used opportunistically to downscale large images.
"""
import base64
import gzip
import http.client
import io
import json
import logging
import sys
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8787
UPSTREAM = "https://apihub.agnes-ai.com"
BASE = Path(__file__).resolve().parent
LOG_PATH = BASE / "proxy.log"
REQDIR = BASE / "requests"
LOG_MAX = 5 * 1024 * 1024
ERR_CAP = 4096
TRANSIENT_400 = b"Generation failed, try later"  # upstream transient marker
RETRY_SLEEP = 2.0


class _ReplayedError:
    """Re-emits an already-consumed upstream error response (for retries)."""

    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self, size=-1) -> bytes:
        # accept both .read() and .read(n) so replayed errors work in the
        # chunked passthrough reader
        b = self._body
        if size is not None and 0 <= size < len(b):
            b = b[:size]
        self._body = self._body[len(b):]
        return b

    def getheaders(self):
        return []

    def close(self):
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOG_PATH, encoding="utf-8")],
)
log = logging.getLogger("agnes-proxy")


def rotate_if_big(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > LOG_MAX:
            path.replace(LOG_PATH.with_suffix(".log.old"))
    except OSError:
        pass


def decompress_body(raw: bytes, headers) -> bytes:
    enc = (headers.get("content-encoding") or "").lower()
    if "gzip" in enc:
        return gzip.decompress(raw)
    return raw


def flatten_namespace_tools(body: dict, tool_names: set) -> int:
    tools = body.get("tools")
    if not isinstance(tools, list):
        return 0
    out = []
    expanded = 0
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "namespace":
            ns = str(tool.get("name", ""))
            if not ns:
                out.append(tool)
                continue
            nested = tool.get("tools")
            if isinstance(nested, list):
                for inner in nested:
                    if isinstance(inner, dict) and inner.get("type") == "function":
                        flat = dict(inner)
                        flat["name"] = f"{ns}.{flat.get('name', '')}"
                        flat["type"] = "function"
                        tool_names.add(flat["name"])
                        out.append(flat)
                    else:
                        out.append(inner)
            else:
                out.append(tool)
            expanded += 1
        else:
            out.append(tool)
    body["tools"] = out
    return expanded


# ---------------------------------------------------------------------------
# Image bridge: Responses API -> chat completions rewrite
# ---------------------------------------------------------------------------

def _part_is_image(part) -> bool:
    if not isinstance(part, dict):
        return False
    ptype = part.get("type")
    if ptype in ("input_image", "image", "image_url"):
        return True
    if ptype == "input_file":
        for k in ("file_data", "file_url", "url", "image_url"):
            v = part.get(k)
            if isinstance(v, str) and v.startswith("data:image/"):
                return True
        return False
    # heuristic: any part carrying a data:image URL
    for k in ("image_url", "file_data", "file_url", "url"):
        v = part.get(k)
        if isinstance(v, str) and v.startswith("data:image/"):
            return True
    return False


def _image_url_value(part: dict) -> str:
    for k in ("image_url", "url"):
        v = part.get(k)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, dict) and v.get("url"):
            return v["url"]
    fd = part.get("file_data") or ""
    if isinstance(fd, str) and fd.startswith("data:image/"):
        return fd
    return ""


IMG_MAX_DIM = 1600      # longest edge after downscale
IMG_B64_MAX = 150_000   # keep base64 payload well under the model's context
IMG_B64_TOTAL_MAX = 120_000  # cap on ALL images' base64 in one request
IMG_PX_TOTAL_MAX = 800_000   # cap on ALL images' pixels in one request
                              # (upstream 400s around >1.07M px / >142K b64 total)

try:
    from PIL import Image as _PILImage
except Exception:
    _PILImage = None


def _maybe_shrink(url: str) -> str:
    """Downscale/quantize a large image data-URL so it cannot blow the model
    context window. Passes through small images, remote URLs and failures."""
    if not isinstance(url, str) or not url.startswith("data:image/"):
        return url
    if len(url) <= IMG_B64_MAX or _PILImage is None:
        return url
    try:
        b64 = url.partition(",")[2]
        img = _PILImage.open(io.BytesIO(base64.b64decode(b64)))
        img.load()
        for _ in range(5):  # halve dimensions until small enough
            w, h = img.size
            scale = min(1.0, IMG_MAX_DIM / max(w, h))
            if scale < 1.0:
                img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                                 _PILImage.LANCZOS)
            best = None
            cands = []
            for quality in (80, 60, 45):
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=quality)
                cands.append(("image/jpeg", buf.getvalue()))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            cands.append(("image/png", buf.getvalue()))
            best = min(cands, key=lambda c: len(c[1]))
            if len(best[1]) * 4 // 3 <= IMG_B64_MAX:
                log.info("shrink_image %dx%d -> %s %d bytes (b64 ~%d)",
                         w, h, best[0], len(best[1]), len(best[1]) * 4 // 3)
                return "data:" + best[0] + ";base64," + \
                    base64.b64encode(best[1]).decode()
            img = img.resize((max(1, w // 2), max(1, h // 2)), _PILImage.LANCZOS)
        log.warning("shrink_image still %d bytes after max effort; passing through",
                    len(best[1]) if best else -1)
        return url
    except Exception as exc:
        log.warning("shrink_image failed: %s", exc)
        return url


def _img_pixels(url: str) -> int:
    """Approximate pixel count of a data:image URL (header sniffing only).
    0 means unknown/remote -> callers should treat it conservatively."""
    import struct
    if not isinstance(url, str) or not url.startswith("data:image/"):
        return 0
    raw = base64.b64decode(url.partition(",")[2])[: (1 << 20)]
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
        w, h = struct.unpack(">II", raw[16:24])
        return w * h
    if (raw[:6] in (b"GIF87a", b"GIF89a")) and len(raw) >= 10:
        w, h = struct.unpack("<HH", raw[6:10])
        return w * h
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" and len(raw) >= 30:
        if raw[12:16] == b"VP8L":
            b0, b1, b2, b3, b4 = raw[21:26]
            w = 1 + (((b2 & 0x3F) << 8) | b1)
            h = 1 + (((b4 & 0xF) << 10) | (b3 << 2) | ((b2 & 0xC0) >> 6))
            return w * h
    if raw[:2] == b"\xff\xd8":  # JPEG: scan for SOF marker
        i, n = 2, len(raw)
        while i + 9 < n:
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                return w * h
            seglen = struct.unpack(">H", raw[i + 2:i + 4])[0]
            i += 2 + seglen
    return 0


def _enforce_image_budget(messages: list) -> None:
    """Images accumulate across a codex session and are re-sent every turn.
    Cap the TOTAL image base64 AND total pixels per request; when over, drop
    the OLDEST images (replace with a text placeholder) and keep the most
    recent. Upstream 400s with "Generation failed" above ~1.07M pixels or
    ~142K total base64 chars per request."""
    total_b64 = 0
    total_px = 0
    images = []  # (msg_idx, content_idx, b64_len, pixels) oldest -> newest
    for mi, msg in enumerate(messages):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for ci, part in enumerate(content):
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                b64_len = len(url) if isinstance(url, str) and url.startswith("data:") else 0
                px = _img_pixels(url)
                if px == 0 and b64_len > 0:
                    px = 400_000  # unknown size: assume a heavy image
                images.append((mi, ci, b64_len, px))
                total_b64 += b64_len
                total_px += px
    over = lambda: total_b64 > IMG_B64_TOTAL_MAX or total_px > IMG_PX_TOTAL_MAX
    if not over():
        return
    dropped = 0
    for mi, ci, b64_len, px in images[:-1]:  # always keep the newest
        if not over():
            break
        messages[mi]["content"][ci] = {"type": "text",
                                        "text": "[image dropped to fit context budget]"}
        total_b64 -= b64_len
        total_px -= px
        dropped += 1
    if dropped:
        log.info("image_budget dropped %d old image(s); b64 ~%d px ~%d",
                 dropped, total_b64, total_px)


def input_has_images(payload: dict) -> bool:
    inp = payload.get("input")
    if isinstance(inp, str):
        return False
    if not isinstance(inp, list):
        return False
    for item in inp:
        if not isinstance(item, dict):
            continue
        itype = item.get("type", "message")
        if itype in ("message", "") or "type" not in item:
            content = item.get("content")
            if isinstance(content, list) and any(_part_is_image(p) for p in content):
                return True
        if itype in ("function_call_output", "custom_tool_call_output"):
            out = item.get("output")
            if isinstance(out, list) and any(_part_is_image(p) for p in out):
                return True
        if _part_is_image(item):
            return True
    return False


def _part_to_chat(part: dict):
    ptype = part.get("type")
    if ptype in ("input_text", "text", "output_text", "refusal"):
        return {"type": "text", "text": part.get("text", "")}
    if ptype in ("input_image", "image", "image_url"):
        url = _image_url_value(part)
        if url:
            return {"type": "image_url", "image_url": {"url": _maybe_shrink(url)}}
        return {"type": "text", "text": "[image part without usable URL]"}
    if ptype == "input_file":
        fd = part.get("file_data") or part.get("file_url") or ""
        if isinstance(fd, str) and fd.startswith("data:image/"):
            return {"type": "image_url", "image_url": {"url": _maybe_shrink(fd)}}
        name = part.get("filename") or "file"
        return {"type": "text", "text": f"[attached file {name} could not be converted]"}
    if ptype in ("thinking", "reasoning", "summary"):
        return None
    return {"type": "text",
            "text": f"[unconvertible content part: {ptype or 'unknown'}]"}


def _tool_args_str(value) -> str:
    """Coerce a function-call arguments value to a JSON string for chat API."""
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def responses_input_to_chat_messages(payload: dict) -> list:
    inp = payload.get("input")
    messages = []
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
        return messages
    last_assistant = None
    for item in inp or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type", "message")
        if itype == "message":
            role = item.get("role") or "user"
            content = item.get("content")
            if isinstance(content, str):
                msg = {"role": role, "content": content}
            elif isinstance(content, list):
                parts = []
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    cp = _part_to_chat(p)
                    if cp is not None:
                        parts.append(cp)
                msg = {"role": role, "content": parts if parts else ""}
            else:
                msg = {"role": role, "content": ""}
            messages.append(msg)
            if role == "assistant":
                last_assistant = msg
        elif itype == "function_call":
            tc = {"id": item.get("call_id") or f"call_{uuid.uuid4().hex[:16]}",
                  "type": "function",
                  "function": {"name": item.get("name", ""),
                               "arguments": _tool_args_str(item.get("arguments"))}}
            if isinstance(last_assistant, dict):
                last_assistant.setdefault("tool_calls", []).append(tc)
            else:
                last_assistant = {"role": "assistant", "content": "", "tool_calls": [tc]}
                messages.append(last_assistant)
        elif itype == "custom_tool_call":
            tc = {"id": item.get("call_id") or f"call_{uuid.uuid4().hex[:16]}",
                  "type": "function",
                  "function": {"name": item.get("name", ""),
                               "arguments": json.dumps(item.get("input", ""),
                                                       ensure_ascii=False)}}
            if isinstance(last_assistant, dict):
                last_assistant.setdefault("tool_calls", []).append(tc)
            else:
                last_assistant = {"role": "assistant", "content": "", "tool_calls": [tc]}
                messages.append(last_assistant)
        elif itype in ("function_call_output", "custom_tool_call_output"):
            out = item.get("output")
            text = ""
            extra_images = []
            if isinstance(out, str):
                text = out
            elif isinstance(out, list):
                texts = []
                for p in out:
                    if not isinstance(p, dict):
                        continue
                    cp = _part_to_chat(p)
                    if cp is None:
                        continue
                    if cp.get("type") == "image_url":
                        extra_images.append(cp)
                    else:
                        t = cp.get("text", "")
                        if t:
                            texts.append(t)
                text = "\n".join(texts)
                if not text and extra_images:
                    # images travel via the following user message; never
                    # json-dump the raw part list (embeds full base64!)
                    text = "[tool returned image content]"
            elif out is not None:
                text = json.dumps(out, ensure_ascii=False)
            messages.append({"role": "tool",
                             "tool_call_id": item.get("call_id", ""),
                             "content": text or ""})
            if extra_images:
                # upstream chat needs tool content to be a string; re-home
                # the image parts into a following user message
                messages.append({"role": "user", "content": extra_images})
        # reasoning / item_reference / other item types: not needed upstream
    return messages


def responses_tools_to_chat(payload: dict) -> list:
    out = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        fn = {"name": tool.get("name", "")}
        if tool.get("description"):
            fn["description"] = tool["description"]
        if "parameters" in tool:
            fn["parameters"] = tool["parameters"]
        out.append({"type": "function", "function": fn})
    return out


def _map_tool_choice(tc):
    if tc in ("auto", "required", "none"):
        return tc
    if isinstance(tc, dict):
        if tc.get("type") == "function" and tc.get("name"):
            return {"type": "function", "function": {"name": tc["name"]}}
    return "auto"


def build_chat_payload(payload: dict) -> dict:
    chat = {"model": payload.get("model", "")}
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        chat["messages"] = [{"role": "system", "content": instructions}]
    else:
        chat["messages"] = []
    chat["messages"].extend(responses_input_to_chat_messages(payload))
    _enforce_image_budget(chat["messages"])
    if payload.get("stream"):
        chat["stream"] = True
        chat["stream_options"] = {"include_usage": True}
    mot = payload.get("max_output_tokens")
    if mot is not None:
        chat["max_tokens"] = int(mot)
    for key in ("temperature", "top_p", "stop"):
        if key in payload and payload[key] is not None:
            chat[key] = payload[key]
    tools = responses_tools_to_chat(payload)
    if tools:
        chat["tools"] = tools
        tc = payload.get("tool_choice")
        chat["tool_choice"] = _map_tool_choice(tc) if tc is not None else "auto"
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        chat["reasoning"] = {"effort": reasoning["effort"]}
    return chat


def _resp_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _usage_from_chat(u) -> dict:
    if not isinstance(u, dict):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    it = u.get("prompt_tokens") or 0
    ot = u.get("completion_tokens") or 0
    return {"input_tokens": it, "output_tokens": ot,
            "total_tokens": u.get("total_tokens") or (it + ot),
            "input_tokens_details": {"cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)},
            "output_tokens_details": {"reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)}}


def _tool_call_items(tc_list: list) -> list:
    items = []
    for tc in tc_list or []:
        fn = (tc or {}).get("function") or {}
        args = fn.get("arguments") or ""
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        items.append({"id": _resp_id("fc"), "type": "function_call",
                     "call_id": (tc or {}).get("id") or _resp_id("call"),
                     "name": fn.get("name", ""), "arguments": args,
                     "status": "completed"})
    return items


def _message_from_chat(msg: dict) -> dict:
    item = {"id": _resp_id("msg"), "type": "message", "role": "assistant",
            "status": "completed", "content": []}
    text = msg.get("content")
    if isinstance(text, str) and text:
        item["content"].append({"type": "output_text", "text": text, "annotations": []})
    return item


def build_responses_object(chat_json: dict, model: str) -> dict:
    choice = (chat_json.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    output = [_message_from_chat(msg)]
    output.extend(_tool_call_items(msg.get("tool_calls")))
    return {"id": _resp_id("resp"), "object": "response", "created_at": 0,
            "model": model, "output": output,
            "status": "completed", "error": None, "incomplete_details": None,
            "usage": _usage_from_chat(chat_json.get("usage"))}


def _input_shapes(payload) -> str:
    """Compact description of input item/part types, for 400 diagnostics."""
    if not isinstance(payload, dict):
        return "payload-not-dict"
    out = []
    inp = payload.get("input")
    if isinstance(inp, str):
        out.append("input=str")
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                out.append(f"item={type(item).__name__}")
                continue
            t = item.get("type", "message")
            content = item.get("content")
            if isinstance(content, list):
                parts = [(p.get("type") if isinstance(p, dict)
                          else type(p).__name__) for p in content]
                out.append(f"{t}:{parts}")
            else:
                out.append(str(t))
    else:
        out.append(f"input={type(inp).__name__}")
    return " | ".join(out[:64]) or "empty"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "agnes-codex-proxy/1.2"

    def log_message(self, fmt, *args):
        log.debug("%s " + fmt, self.address_string(), *args)

    def _sse(self, event_type: str, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.wfile.write(b"event: " + event_type.encode() + b"\ndata: " + data + b"\n\n")
        self.wfile.flush()

    def _send_image_bridge_response(self, upstream, model: str, stream: bool) -> int:
        if upstream.status >= 400:
            err_body = upstream.read()
            self.send_response(upstream.status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(err_body)))
            self.send_header("connection", "close")
            self.end_headers()
            self.wfile.write(err_body)
            self.wfile.flush()
            log.info("RESP_HTTP_%d (image bridge) body=%s", upstream.status,
                     err_body[:ERR_CAP].decode("utf-8", "replace"))
            return len(err_body)

        if not stream:
            raw = upstream.read()
            try:
                chat_json = json.loads(raw.decode("utf-8"))
            except Exception:
                chat_json = {}
            body = json.dumps(build_responses_object(chat_json, model),
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.send_header("connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            return len(body)

        # streaming: synthesize Responses-API SSE events from chat chunks
        resp_id = _resp_id("resp")
        msg_id = _resp_id("msg")
        self.send_response(200)
        for key, val in upstream.getheaders():
            if key.lower() in ("connection", "transfer-encoding", "content-length",
                               "keep-alive", "proxy-connection"):
                continue
            self.send_header(key, val)
        self.send_header("content-type", "text/event-stream")
        self.send_header("connection", "close")
        self.end_headers()

        skeleton = {"id": resp_id, "object": "response", "status": "in_progress",
                    "model": model, "output": []}
        self._sse("response.created", {"type": "response.created", "response": skeleton})
        item_skel = {"id": msg_id, "type": "message", "role": "assistant",
                     "status": "in_progress", "content": []}
        self._sse("response.output_item.added",
                   {"type": "response.output_item.added", "item": item_skel})

        text_buf = []
        tc_acc = {}
        tc_auto = 1000      # auto-assigned keys for index-less new calls
        tc_last = None      # most recent key (for continuation deltas)
        usage = None
        total = 0
        while True:
            line = upstream.readline()
            if not line:
                break
            line = line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except Exception:
                continue
            total += len(line)
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if choice.get("finish_reason"):
                    pass
                dtext = delta.get("content")
                if dtext:
                    text_buf.append(dtext)
                    self._sse("response.output_text.delta",
                               {"type": "response.output_text.delta",
                                "item_id": msg_id, "output_index": 0,
                                "content_index": 0, "delta": dtext})
                for dtc in (delta.get("tool_calls") or []):
                    # Robust tool-call keying across provider styles:
                    #  - use the delta's `index` when present (standard)
                    #  - a delta with a NEW `id` is a new call -> auto key
                    #  - a bare continuation (no index, no id) -> last key
                    pidx = dtc.get("index")
                    if isinstance(pidx, int) and pidx >= 0:
                        key = pidx
                    elif dtc.get("id"):
                        key = tc_auto
                        tc_auto += 1
                    else:
                        if tc_last is None:
                            tc_last = tc_auto
                            tc_auto += 1
                        key = tc_last
                    tc_last = key
                    entry = tc_acc.setdefault(
                        key, {"id": "", "type": "function",
                              "function": {"name": "", "arguments": ""}})
                    if dtc.get("id"):
                        entry["id"] = dtc["id"]
                    fn = dtc.get("function") or {}
                    name = fn.get("name")
                    if name:
                        cur_name = entry["function"]["name"]
                        if not cur_name:
                            entry["function"]["name"] = name
                        elif cur_name == name:
                            pass  # provider repeated the full name; don't dup
                        else:
                            entry["function"]["name"] = cur_name + name
                    if fn.get("arguments"):
                        entry["function"]["arguments"] += fn["arguments"]

        final_item = dict(item_skel)
        final_item["status"] = "completed"
        full_text = "".join(text_buf)
        if full_text:
            final_item["content"] = [{"type": "output_text", "text": full_text,
                                      "annotations": []}]
        self._sse("response.output_item.done",
                   {"type": "response.output_item.done", "item": final_item})
        fc_items = []
        for idx in sorted(tc_acc):
            e = tc_acc[idx]
            fc = {"id": _resp_id("fc"), "type": "function_call",
                  "call_id": e.get("id") or _resp_id("call"),
                  "name": e["function"].get("name", ""),
                  "arguments": e["function"].get("arguments") or "{}",
                  "status": "completed"}
            self._sse("response.output_item.added",
                       {"type": "response.output_item.added", "item": fc})
            self._sse("response.output_item.done",
                       {"type": "response.output_item.done", "item": fc})
            fc_items.append(fc)
        completed = dict(skeleton)
        completed.update({"status": "completed",
                          "output": [final_item] + fc_items,
                          "error": None, "incomplete_details": None,
                          "usage": _usage_from_chat(usage)})
        self._sse("response.completed",
                   {"type": "response.completed", "response": completed})
        return total

    def handle_request(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, parsed.query
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""

        req_headers = {k: v for k, v in self.headers.items()}
        tool_names: set = set()
        had_namespace = 0
        upstream_body = raw
        bridge = None
        bridge_model = ""
        last_payload = None

        if method in ("POST", "PUT", "PATCH") and raw:
            content_type = (self.headers.get("content-type") or "").lower()
            if "json" in content_type:
                try:
                    payload = json.loads(decompress_body(raw, self.headers))
                    if isinstance(payload, dict):
                        last_payload = payload
                        had_namespace = flatten_namespace_tools(payload, tool_names)
                        tc = payload.get("tool_choice")
                        if tc is not None and not payload.get("tools"):
                            payload.pop("tool_choice")
                            log.info("stripped tool_choice (no tools); was %s", tc)
                        if path.endswith("/responses") and input_has_images(payload):
                            chat = build_chat_payload(payload)
                            upstream_body = json.dumps(chat, ensure_ascii=False).encode("utf-8")
                            bridge = "stream" if chat.get("stream") else "json"
                            bridge_model = chat.get("model", "")
                            log.info("IMAGE BRIDGE responses->chat (%s, model=%s)",
                                     bridge, bridge_model)
                            try:
                                REQDIR.mkdir(exist_ok=True)
                                (REQDIR / f"{int(time.time())}_chatbridge.json").write_bytes(
                                    upstream_body[:2_000_000])
                            except OSError:
                                pass
                        else:
                            upstream_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                        req_headers.pop("content-encoding", None)
                except Exception as exc:
                    log.warning("request parse failed (%s); passing raw through", exc)

        host = urllib.parse.urlparse(UPSTREAM).netloc
        conn = http.client.HTTPSConnection(host, timeout=600)
        headers = {}
        for key, val in req_headers.items():
            lk = key.lower()
            if lk in ("host", "content-length", "connection", "transfer-encoding",
                      "keep-alive", "proxy-connection"):
                continue
            headers[key] = val
        headers["host"] = host

        if bridge:
            url = UPSTREAM + "/v1/chat/completions"
        else:
            url = UPSTREAM + path + (("?" + query) if query else "")

        if bridge:
            headers["content-length"] = str(len(upstream_body))
            upstream = None
            # Hardened retry: upstream "Generation failed" 400s and 5xx
            # (incl. the hub's own 500 bad_response_body during backend
            # wobbles) are transient. Back off exponentially up to ~80s
            # before surfacing the error to the client.
            max_attempts = 6
            for attempt in range(max_attempts):
                bconn = http.client.HTTPSConnection(host, timeout=600)
                bconn.request("POST", url, body=upstream_body, headers=headers)
                resp = bconn.getresponse()
                transient = False
                errb = b""
                if resp.status >= 400:
                    errb = resp.read()
                    resp.close()
                    bconn.close()
                    if TRANSIENT_400 in errb or 500 <= resp.status <= 599:
                        transient = attempt < max_attempts - 1
                else:
                    upstream = resp
                    break
                if transient:
                    delay = min(60.0, RETRY_SLEEP * (2 ** attempt))
                    why = "transient 400" if TRANSIENT_400 in errb else "5xx %d" % resp.status
                    log.info("IMAGE BRIDGE %s (attempt %d/%d), retry in %.0fs",
                             why, attempt + 1, max_attempts, delay)
                    time.sleep(delay)
                    continue
                upstream = _ReplayedError(resp.status, errb)
                break
            total = self._send_image_bridge_response(upstream, bridge_model, bridge == "stream")
            upstream.close()
            bconn.close()
        else:
            if upstream_body:
                headers["content-length"] = str(len(upstream_body))
            # Passthrough retry: the hub's /v1/responses flaps 5xx during
            # backend wobbles and occasionally returns the opaque
            # "Generation failed" 400. Back off (up to ~80s) before
            # surfacing the error; non-transient 4xx fail fast.
            max_attempts = 6
            upstream = None
            for attempt in range(max_attempts):
                conn = http.client.HTTPSConnection(host, timeout=600)
                conn.request(method, url,
                             body=upstream_body if upstream_body else None,
                             headers=headers)
                resp = conn.getresponse()
                transient = False
                if resp.status >= 400:
                    errb = resp.read()
                    resp.close()
                    conn.close()
                    if (TRANSIENT_400 in errb or 500 <= resp.status <= 599) \
                            and attempt < max_attempts - 1:
                        transient = True
                    if transient:
                        delay = min(60.0, RETRY_SLEEP * (2 ** attempt))
                        why = ("transient 400" if TRANSIENT_400 in errb
                               else "5xx %d" % resp.status)
                        log.info("PASSTHROUGH %s (attempt %d/%d), retry in %.0fs",
                                 why, attempt + 1, max_attempts, delay)
                        time.sleep(delay)
                        continue
                    upstream = _ReplayedError(resp.status, errb)
                    break
                else:
                    upstream = resp
                    break
            if upstream is None:
                upstream = _ReplayedError(502, b'{"error":{"message":"upstream unavailable after retries"}}')

            self.send_response(upstream.status)
            for key, val in upstream.getheaders():
                lk = key.lower()
                if lk in ("connection", "transfer-encoding", "content-length",
                          "keep-alive", "proxy-connection"):
                    continue
                self.send_header(key, val)
            self.send_header("connection", "close")
            self.end_headers()

            total = 0
            captured = b""
            capture_all = upstream.status >= 400
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                total += len(chunk)
                if capture_all and len(captured) < ERR_CAP:
                    captured += chunk[: ERR_CAP - len(captured)]
            upstream.close()
            conn.close()

            if captured:
                log.info("RESP_HTTP_%d body=%s", upstream.status,
                         captured.decode("utf-8", "replace"))

            if upstream.status >= 400 and path.endswith("/responses"):
                try:
                    REQDIR.mkdir(exist_ok=True)
                    (REQDIR / f"{int(time.time())}_resp400.json").write_bytes(
                        upstream_body[:500_000])
                    log.info("RESP400 saved request; input shapes: %s",
                             _input_shapes(last_payload))
                except OSError:
                    pass

        if had_namespace:
            log.info("FLATTENED %d namespace entr(ies); names=%s",
                     had_namespace, sorted(tool_names)[:32])
            try:
                REQDIR.mkdir(exist_ok=True)
                (REQDIR / f"{int(time.time())}.json").write_bytes(
                    upstream_body[:500_000])
            except OSError:
                pass
        log.info("%s %s%s status=%d out_bytes=%d ns_expanded=%d",
                 method, path, ("?" + query if query else ""),
                 (upstream.status if not bridge else 200), total, had_namespace)

    def do_POST(self):
        self.handle_request("POST")

    def do_GET(self):
        self.handle_request("GET")

    def do_PUT(self):
        self.handle_request("PUT")

    def do_PATCH(self):
        self.handle_request("PATCH")

    def do_DELETE(self):
        self.handle_request("DELETE")


def main() -> None:
    REQDIR.mkdir(parents=True, exist_ok=True)
    rotate_if_big(LOG_PATH)
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        log.error("cannot bind %s:%s (%s) - another instance running?", HOST, PORT, exc)
        return
    log.info("listening on http://%s:%d -> %s", HOST, PORT, UPSTREAM)
    server.serve_forever()


if __name__ == "__main__":
    main()

