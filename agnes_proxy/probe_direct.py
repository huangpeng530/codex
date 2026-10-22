#!/usr/bin/env python3
"""Direct-connect capability probe for retiring the agnes_proxy workaround.

Checks whether the Agnes hub + current app engine still need the local proxy:

  [1] namespace : does POST /v1/responses accept `type:"namespace"` tools?
  [2] image     : does POST /v1/responses keep `input_image` parts?
  [3] engine    : does the app's bundled codex engine still EMIT
                  `type:"namespace"` tools (via a local capture server)?

The proxy can be retired when [1] is ACCEPTED and [2] is OK (unless the user
no longer sends images). Runs stdlib-only; safe to run repeatedly.

Usage:
    python probe_direct.py                 # full probe
    python probe_direct.py --no-engine     # skip the engine capture
    python probe_direct.py --engine-exe C:\\path\\codex.exe
    python probe_direct.py --hub https://apihub.agnes-ai.com/v1 --model agnes-3.0-flash

Exit codes: 0 = proxy no longer needed, 1 = proxy still required, 2 = probe error.
"""
import argparse
import base64
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HUB_DEFAULT = "https://apihub.agnes-ai.com/v1"
MODEL_DEFAULT = "agnes-3.0-flash"
KEY_ENV = "AGNES_API_KEY"
ENGINE_DIR_HINT = os.path.join(os.environ.get("LOCALAPPDATA", ""), "OpenAI", "Codex", "bin")


# --------------------------------------------------------------------------- png

def _tiny_red_png() -> bytes:
    """Build a valid 1x1 red PNG in memory (stdlib only)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\xff\x00\x00"
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


# ------------------------------------------------------------------- http helper

def post_json(url: str, api_key: str, payload: dict, timeout: float, retries: int = 3):
    """POST JSON with transient-error retries (GFW resets kill in-flight TLS)."""
    last = None
    for i in range(retries):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()          # a hub 4xx is the signal we want
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if i + 1 < retries:
                time.sleep(2.0)
    raise ConnectionError(f"unreachable {url}: {last}")


def error_message(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message", body))[:200]
        return json.dumps(data)[:200]
    except Exception:
        return body.decode("utf-8", "replace")[:200]


# ------------------------------------------------------------- engine capture

def find_app_engine() -> str | None:
    root = Path(ENGINE_DIR_HINT)
    cands = [p for p in root.glob("*/codex.exe") if p.exists()]
    if not cands:
        return None
    return str(max(cands, key=lambda p: p.parent.stat().st_mtime))


def capture_engine_request(exe: str, port: int, model: str) -> dict:
    """Run the engine's `codex exec` against a local capture server on `port`.

    Returns {"found": bool, "namespaces": [...], "version": str}."""
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            captured["body"] = body
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"probe-capture","type":"invalid_request_error"}}')

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{port}/v1"
    args = [
        exe, "exec",
        "-c", f"model={model}",
        "-c", "model_provider=agnes",
        "-c", f"model_providers.agnes.base_url={base_url}",
        "-c", f"model_providers.agnes.env_key={KEY_ENV}",
        "-c", "model_providers.agnes.wire_api=responses",
        "-c", "web_search=disabled",
        "-c", "features.code_mode=true",
        "reply with the single word ok",
    ]
    try:
        proc = subprocess.run(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=90, check=False,
        )
    except subprocess.TimeoutExpired:
        proc = None
    server.shutdown()
    out = proc.stdout.decode("utf-8", "replace") if proc else ""
    version = ""
    for line in out.splitlines():
        if re.search(r"v?\d+\.\d+\.\d+", line) and ("Codex" in line or "codex" in line):
            version = line.strip()
            break
    result = {"found": "body" in captured, "namespaces": [], "version": version}
    if result["found"]:
        try:
            data = json.loads(captured["body"])
            tools = data.get("tools", []) if isinstance(data, dict) else []
            result["namespaces"] = [
                t.get("name", "?") for t in tools
                if isinstance(t, dict) and t.get("type") == "namespace"
            ]
        except Exception:
            pass
    return result


# ------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hub", default=HUB_DEFAULT, help=f"hub base url (default {HUB_DEFAULT})")
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--no-engine", action="store_true", help="skip the engine capture probe")
    ap.add_argument("--no-images", action="store_true", help="user no longer sends images; ignore [2]")
    ap.add_argument("--engine-exe", help="explicit engine path (default: newest app bin)")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    api_key = os.environ.get(KEY_ENV, "")
    if not api_key:
        print(f"ERROR: {KEY_ENV} not set in the environment; cannot probe the hub.")
        return 2

    results = {}

    # [1] namespace tools
    ns_payload = {
        "model": args.model,
        "input": [{"role": "user",
                   "content": [{"type": "input_text", "text": "Reply with the single word: ok"}]}],
        "tools": [{
            "type": "namespace", "name": "probe_ns", "description": "capability probe",
            "tools": [{"type": "function", "name": "f", "description": "probe fn",
                        "parameters": {"type": "object", "properties": {}, "required": []}}],
        }],
        "tool_choice": "none",
        "max_output_tokens": 8,
    }
    try:
        status, body = post_json(f"{args.hub}/responses", api_key, ns_payload, args.timeout)
        if 200 <= status < 300:
            results["namespace"] = "ACCEPTED"
        else:
            msg = error_message(body)
            results["namespace"] = "REJECTED"
            results["namespace_detail"] = f"HTTP {status}: {msg}"
    except ConnectionError as e:
        results["namespace"] = "UNKNOWN"
        results["namespace_detail"] = str(e)

    # [2] image parts
    if args.no_images:
        results["image"] = "SKIPPED"
    else:
        b64 = base64.b64encode(_tiny_red_png()).decode()
        img_payload = {
            "model": args.model,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "What color is the pixel in this image? One word."},
                {"type": "input_image", "image_url": f"data:image/png;base64,{b64}", "detail": "low"},
            ]}],
            "tool_choice": "none",
            "max_output_tokens": 8,
        }
        try:
            status, body = post_json(f"{args.hub}/responses", api_key, img_payload, args.timeout)
            if 200 <= status < 300:
                results["image"] = "OK"
            else:
                msg = error_message(body)
                results["image"] = "REJECTED"
                results["image_detail"] = f"HTTP {status}: {msg}"
        except ConnectionError as e:
            results["image"] = "UNKNOWN"
            results["image_detail"] = str(e)

    # [3] engine output
    if args.no_engine:
        results["engine"] = "SKIPPED"
    else:
        exe = args.engine_exe or find_app_engine()
        if not exe:
            results["engine"] = "UNKNOWN"
            results["engine_detail"] = "no app engine found under %LOCALAPPDATA%\\OpenAI\\Codex\\bin"
        else:
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            info = capture_engine_request(exe, port, args.model)
            results["engine"] = "EMITTING_NAMESPACE" if info["namespaces"] else "NOT_EMITTING"
            results["engine_detail"] = (
                f"{exe} ({info['version'] or 'version ?'}) -> namespaces: "
                f"{', '.join(info['namespaces']) if info['namespaces'] else 'none'}"
            )

    # ------------------------------------------------------------ verdict
    ns_ok = results["namespace"] == "ACCEPTED"
    img_ok = results.get("image") in ("OK", "SKIPPED")
    unknowns = [k for k, v in results.items() if v == "UNKNOWN" and not k.endswith("_detail")]

    print("== Agnes direct-connect probe ==")
    print(f"hub: {args.hub}  model: {args.model}")
    print(f"[1] namespace tools in /v1/responses : {results['namespace']}")
    if "namespace_detail" in results:
        print(f"      {results['namespace_detail']}")
    print(f"[2] image parts in /v1/responses      : {results.get('image')}")
    if "image_detail" in results:
        print(f"      {results['image_detail']}")
    print(f"[3] app engine namespace output       : {results.get('engine')}")
    if "engine_detail" in results:
        print(f"      {results['engine_detail']}")
    print()
    if unknowns:
        print(f"PROBE INCOMPLETE ({', '.join(unknowns)} unresolved) - re-run when the network recovers.")
        return 2
    if ns_ok and img_ok:
        print("VERDICT: proxy NOT needed - the hub now handles namespaces/images in /v1/responses.")
        print("        代理可以撤除：改 config.toml 的 base_url 直连 hub，然后 install.ps1 -Unregister。")
        return 0
    print("VERDICT: proxy STILL REQUIRED.")
    if not ns_ok:
        print("        - hub still rejects type:\"namespace\" (or engine still emits it where it matters)")
    if results.get("image") == "REJECTED":
        print("        - hub /v1/responses still drops/rejects image parts")
    print("        Re-run this probe after each app/hub upgrade; keep agnes_proxy until all pass.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
