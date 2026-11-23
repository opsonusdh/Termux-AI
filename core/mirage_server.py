"""Mirage as a local OpenAI-compatible endpoint.

Mirage — Cactus Compute's 21.6 MB vision-to-text model — cannot load through
the ``cactus-compute`` package on Termux: the package only ships glibc/musl
engines, and Bionic has none (``cactus.agent.fetch._platform_tag`` raises).
What *is* published for Android is the engine compiled to WebAssembly
(``cactus.js`` + ``cactus.wasm``, release 4.1.4 — the exact engine generation
the pinned ``mirage.cact`` targets), and it exposes the complete ``cactus_*``
C ABI — including ``cactus_describe`` — to Node.js.

This module turns that into a small service:

* a Node bridge child process keeps the engine and the model resident and
  answers line-JSON requests (model load costs seconds, so it must persist);
* a loopback HTTP server speaks the OpenAI chat-completions protocol on
  ``/v1/chat/completions`` (plus ``/v1/models`` and ``/health``), so anything
  that talks OpenAI — the ``openai`` SDK included — can use Mirage:

      from openai import OpenAI
      client = OpenAI(base_url="http://127.0.0.1:8471/v1", api_key="local")
      reply = client.chat.completions.create(
          model="mirage",
          messages=[{"role": "user", "content": [
              {"type": "text", "text": "What is in this picture?"},
              {"type": "image_url", "image_url": {"url": data_uri}}]}])
      print(reply.choices[0].message.content)

* :func:`ensure_endpoint` is the hook ``core.llm_client`` calls when the
  ``cactus`` import fails: it reuses a running server, or starts one, and
  hands back a client whose ``describe(image)`` matches the package API.

Only the standard library is used; Pillow is needed to decode picture files.
"""

import argparse
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Path bootstrap (core/ first, root second — mirrors every core module).
_CORE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_CORE)
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)
if _ROOT not in sys.path:
    sys.path.insert(1, _ROOT)

import paths  # noqa: E402

MODEL_NAME = "mirage"
# The engine emits structured JSON (scene/objects/OCR/masks) inside a buffer
# sized like the package's own 16 MB describe buffer.
_OUT_BUFFER = 16 * 1024 * 1024
# A cold wasm describe measured ~6 s on-device; a large screenshot with OCR
# needs more, and a cold server adds model load on top.
DESCRIBE_TIMEOUT = 300.0
STARTUP_TIMEOUT = 120.0

# ── Node bridge (embedded; materialised beside the engine on first start) ────

_BRIDGE_JS = r"""
// Line-JSON bridge between Python and the Cactus 4.1.4 wasm engine.
// Usage: node mirage_bridge.js <dir-with-cactus.js> <mirage.cact>
// stdin : one JSON object per line {id, rgb: <base64>, w, h, options?, language?, keywords?}
// stdout: one JSON object per reply  {id, ok, result?|error?}, then nothing else.
const fs = require('fs');
const dir = process.argv[2];
const modelPath = process.argv[3];
const createCactus = require(dir + '/cactus.js');

function send(obj) { process.stdout.write(JSON.stringify(obj) + '\n'); }

(async () => {
  let mod;
  try {
    mod = await createCactus();
  } catch (err) {
    send({ event: 'ready', ok: false, error: 'engine load failed: ' + err.message });
    process.exit(2);
  }
  const model = fs.readFileSync(modelPath);
  const mp = mod._malloc(model.length);
  new Uint8Array(mod.HEAPU8.buffer, mp, model.length).set(model);
  if (mod._cactus_load(mp, BigInt(model.length)) < 0) {
    send({ event: 'ready', ok: false, error: 'cactus_load failed: ' + mod._cactus_last_error() });
    process.exit(2);
  }
  if (!(mod._cactus_models() & 4)) {
    send({ event: 'ready', ok: false, error: 'not a Mirage vision model' });
    process.exit(2);
  }
  const outCap = 16777216;
  const out = mod._malloc(outCap);
  const optBuf = mod._malloc(65536);
  send({ event: 'ready', ok: true });

  const readline = require('readline').createInterface({ input: process.stdin });
  readline.on('line', (line) => {
    line = line.trim();
    if (!line) return;
    let req;
    try { req = JSON.parse(line); } catch (err) {
      send({ id: null, ok: false, error: 'bad request line: ' + err.message });
      return;
    }
    try {
      const rgb = Buffer.from(req.rgb, 'base64');
      const rp = mod._malloc(rgb.length);
      new Uint8Array(mod.HEAPU8.buffer, rp, rgb.length).set(rgb);
      const optBytes = Buffer.from(JSON.stringify(req.options || {}) + '\0', 'utf8');
      new Uint8Array(mod.HEAPU8.buffer, optBuf, optBytes.length).set(optBytes);
      const lang = req.language ? Buffer.from(req.language + '\0', 'utf8') : null;
      const langPtr = lang ? mod._malloc(lang.length) : 0;
      if (lang) new Uint8Array(mod.HEAPU8.buffer, langPtr, lang.length).set(lang);
      const kw = req.keywords ? Buffer.from(req.keywords + '\0', 'utf8') : null;
      const kwPtr = kw ? mod._malloc(kw.length) : 0;
      if (kw) new Uint8Array(mod.HEAPU8.buffer, kwPtr, kw.length).set(kw);
      const rc = mod._cactus_describe(rp, req.w, req.h, optBuf, langPtr, kwPtr, out, outCap);
      if (kwPtr) mod._free(kwPtr);
      if (langPtr) mod._free(langPtr);
      mod._free(rp);
      if (rc < 0) {
        let end = out; while (mod.HEAPU8[end] !== 0) end++;
        const detail = Buffer.from(mod.HEAPU8.subarray(out, end)).toString('utf8');
        send({ id: req.id, ok: false, error: detail || ('cactus_describe failed: ' + rc) });
        return;
      }
      let end = out; while (mod.HEAPU8[end] !== 0) end++;
      const text = Buffer.from(mod.HEAPU8.subarray(out, end)).toString('utf8');
      send({ id: req.id, ok: true, result: JSON.parse(text) });
    } catch (err) {
      send({ id: req.id, ok: false, error: String(err && err.message || err) });
    }
  });
})();
"""


def _bridge_source() -> str:
    return _BRIDGE_JS


class BridgeError(RuntimeError):
    """The node bridge could not start or a describe call failed."""


class MirageBridge:
    """A persistent Node child process holding the wasm engine and the model."""

    def __init__(self, model_path: str | None = None, bridge_path: str | None = None,
                 verbose: bool = False):
        self.model_path = model_path or paths.MIRAGE_MODEL
        self.bridge_path = bridge_path or paths.MIRAGE_BRIDGE
        self.verbose = verbose
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._pending: dict[str, list] = {}
        self._next_id = 0
        self._ready = False
        self._last_error: str | None = None

    def _debug(self, message: str) -> None:
        if self.verbose:
            print(f"[mirage-bridge] {message}", file=sys.stderr, flush=True)

    # -- lifecycle ----------------------------------------------------------

    def _ensure_bridge_file(self) -> None:
        os.makedirs(os.path.dirname(self.bridge_path), exist_ok=True)
        if not os.path.isfile(self.bridge_path):
            with open(self.bridge_path, "w", encoding="utf-8") as handle:
                handle.write(_bridge_source())

    def _engine_ok(self) -> str | None:
        for path in (self.model_path, paths.MIRAGE_WASM_JS, paths.MIRAGE_WASM):
            if not os.path.isfile(path):
                return path
        return None

    def start(self) -> None:
        missing = self._engine_ok()
        if missing:
            raise BridgeError(f"engine artifact missing: {missing}")
        node = shutil.which("node")
        if not node:
            raise BridgeError("node is not installed (pkg install nodejs)")
        self._ensure_bridge_file()
        os.makedirs(paths.LOGS_DIR, exist_ok=True)
        self._proc = subprocess.Popen(
            [node, self.bridge_path, paths.MIRAGE_DIR, self.model_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0,
            start_new_session=True)
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._stderr_drain, daemon=True).start()
        deadline = time.time() + STARTUP_TIMEOUT
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise BridgeError(self._last_error or
                                  f"bridge exited with code {self._proc.returncode}")
            with self._lock:
                if self._ready:
                    return
            time.sleep(0.2)
        self.stop()
        raise BridgeError("bridge did not become ready in time")

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.stdin.close()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 — a dying bridge is torn down hard
                proc.kill()

    # -- pipes --------------------------------------------------------------

    def _stderr_drain(self):
        proc = self._proc
        if not proc or not proc.stderr:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._debug("engine stderr: " + line[:200])
                    self._last_error = line
        except Exception as error:  # noqa: BLE001 — the pipe died with the bridge
            self._debug(f"stderr drain stopped: {error}")

    def _reader(self):
        proc = self._proc
        if not proc or not proc.stdout:
            return
        self._debug("reader thread started")
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                self._debug(f"got {len(line)}B: {line[:120]}")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._debug("unparseable line dropped")
                    continue
                self._dispatch(message)
        except Exception as error:  # noqa: BLE001 — the pipe died with the bridge
            self._debug(f"reader stopped: {error}")

    def _dispatch(self, message: dict) -> None:
        if message.get("event") == "ready":
            with self._lock:
                self._ready = bool(message.get("ok"))
                if not self._ready:
                    self._last_error = message.get("error")
            return
        request_id = message.get("id")
        with self._lock:
            waiter = self._pending.pop(request_id, None)
        # The waiter is an empty list until the reply lands — test for None,
        # never truthiness, or every reply is dropped as "unknown id".
        if waiter is not None:
            waiter.append(message)
        else:
            self._debug(f"reply for unknown id {request_id!r}")

    # -- calls --------------------------------------------------------------

    def describe(self, rgb: bytes, width: int, height: int,
                 options: dict | None = None,
                 language: str | None = None, keywords: str | None = None) -> dict:
        """One cactus_describe call; returns the engine's parsed JSON."""
        with self._lock:
            if not self._ready or self._proc is None or self._proc.poll() is not None:
                raise BridgeError("bridge is not running")
            self._next_id += 1
            request_id = str(self._next_id)
            waiter: list = []
            self._pending[request_id] = waiter
        request = {
            "id": request_id,
            "rgb": base64.b64encode(rgb).decode("ascii"),
            "w": width, "h": height,
            "options": options or {},
            "language": language,
            "keywords": keywords,
        }
        try:
            self._proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
            self._proc.stdin.flush()
        except Exception as error:  # noqa: BLE001 — a dead pipe kills the call
            with self._lock:
                self._pending.pop(request_id, None)
            raise BridgeError(f"bridge pipe broke: {error}") from error
        deadline = time.time() + DESCRIBE_TIMEOUT
        while time.time() < deadline:
            if waiter:
                message = waiter[0]
                if not message.get("ok"):
                    raise BridgeError(str(message.get("error")))
                return message.get("result") or {}
            if self._proc.poll() is not None:
                with self._lock:
                    self._pending.pop(request_id, None)
                raise BridgeError(self._last_error or "bridge exited during describe")
            time.sleep(0.05)
        with self._lock:
            self._pending.pop(request_id, None)
        raise BridgeError("describe timed out")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False


# ── Picture decoding and prose rendering ─────────────────────────────────────

def decode_image(image) -> tuple[bytes, int, int]:
    """Decode a path / bytes / data-URI into packed RGB the engine expects."""
    if isinstance(image, (bytes, bytearray)):
        payload = bytes(image)
    elif isinstance(image, str):
        if image.startswith("data:") and ";base64," in image:
            payload = base64.b64decode(image.partition(";base64,")[2])
        else:
            with open(os.fspath(image), "rb") as handle:
                payload = handle.read()
    else:
        raise ValueError("image must be a path, bytes or a data URI")
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(payload)) as opened:
        rgb = ImageOps.exif_transpose(opened).convert("RGB")
    return rgb.tobytes(), rgb.width, rgb.height


def prose_from_result(result: dict) -> str:
    """Compact prose from the engine's structured JSON, for a chat message."""
    lines: list[str] = []
    scene = result.get("scene")
    if isinstance(scene, dict):
        bits = [str(v) for v in scene.values() if v]
        if bits:
            lines.append("Scene: " + ", ".join(bits) + ".")
    for obj in result.get("objects") or []:
        if not isinstance(obj, dict):
            continue
        bits = [str(obj.get(k)) for k in ("name", "color", "shape")
                if obj.get(k)]
        where = obj.get("position")
        text = " ".join(bits)
        if where:
            text += f" ({where})"
        if text.strip():
            lines.append("- " + text.strip())
    texts = result.get("text")
    if isinstance(texts, list):
        ocr = []
        for entry in texts:
            if isinstance(entry, dict) and entry.get("text"):
                ocr.append(str(entry["text"]))
            elif isinstance(entry, str) and entry:
                ocr.append(entry)
        if ocr:
            lines.append("Visible text: " + " | ".join(ocr))
    for key in ("relations", "events"):
        group = result.get(key)
        if isinstance(group, list) and group:
            lines.append(f"{key.capitalize()}: " + "; ".join(
                str(g) for g in group[:8]))
    return "\n".join(line for line in lines if line.strip()) or json.dumps(
        result, ensure_ascii=False)


# ── The HTTP server ──────────────────────────────────────────────────────────

class _ServerState:
    """Shared bridge + metadata for the request handler."""

    def __init__(self, bridge: MirageBridge):
        self.bridge = bridge
        self.started = time.time()
        self.calls = 0
        self._call_lock = threading.Lock()

    def describe(self, rgb: bytes, width: int, height: int,
                 options: dict | None) -> dict:
        # The engine serves one call at a time; serialize to keep replies sane.
        with self._call_lock:
            self.calls += 1
            return self.bridge.describe(rgb, width, height, options)


def _make_handler(state: _ServerState):
    class MirageHandler(BaseHTTPRequestHandler):
        server_version = "mirage-endpoint/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        # -- helpers ---------------------------------------------------------

        def _json(self, code: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _sse(self, event: str) -> None:
            """One SSE event inside a chunked-transfer body (HTTP/1.1 has no
            Content-Length here, and without framing a client would wait for
            a connection close that never comes under keep-alive)."""
            data = ("data: " + event + "\n\n").encode("utf-8")
            self.wfile.write(f"{len(data):x}\r\n".encode("ascii")
                             + data + b"\r\n")
            self.wfile.flush()

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw or b"{}")

        def _error(self, code: int, message: str) -> None:
            self._json(code, {"error": {"message": message,
                                        "type": "invalid_request_error",
                                        "code": str(code)}})

        # -- routes ----------------------------------------------------------

        def do_GET(self):
            if self.path in ("/health", "/"):
                self._json(200, {"status": "ok", "model": MODEL_NAME,
                                 "calls": state.calls,
                                 "uptime_s": round(time.time() - state.started, 1)})
            elif self.path.rstrip("/") == "/v1/models":
                self._json(200, {"object": "list", "data": [{
                    "id": MODEL_NAME, "object": "model",
                    "created": int(state.started),
                    "owned_by": "cactus-compute"}]})
            else:
                self._error(404, f"unknown path {self.path}")

        def do_POST(self):
            if self.path.rstrip("/") != "/v1/chat/completions":
                self._error(404, f"unknown path {self.path}")
                return
            try:
                request = self._body()
            except json.JSONDecodeError as error:
                self._error(400, f"invalid JSON body: {error}")
                return
            image, options = _extract_image(request)
            if image is None:
                self._error(400,
                            "no image in the request: Mirage is a vision model — "
                            "send an image_url part (data URI or local path)")
                return
            try:
                rgb, width, height = decode_image(image)
            except Exception as error:  # noqa: BLE001 — surfaced as a 400
                self._error(400, f"cannot decode image: {error}")
                return
            try:
                result = state.describe(rgb, width, height, options)
            except BridgeError as error:
                self._json(502, {"error": {"message": str(error),
                                           "type": "engine_error"}})
                return
            content = prose_from_result(result)
            self._reply(request, content, result)

        def _reply(self, request: dict, content: str, raw: dict) -> None:
            created = int(time.time())
            completion_id = f"chatcmpl-mirage-{created}"
            stream = bool(request.get("stream"))
            model = request.get("model") or MODEL_NAME
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for delta, finish in (({"role": "assistant"}, None),
                                      ({"content": content}, None),
                                      ({}, "stop")):
                    self._sse(json.dumps({
                        "id": completion_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{"index": 0, "delta": delta,
                                     "finish_reason": finish}]},
                        ensure_ascii=False))
                self._sse("[DONE]")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                return
            self._json(200, {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": max(1, len(content.split())),
                    "total_tokens": max(1, len(content.split())),
                },
                # Non-standard: the engine's full structured result (masks,
                # boxes, scores) for callers that want more than the prose.
                "mirage_raw": raw,
            })

    return MirageHandler


def _extract_image(request: dict) -> tuple[str | None, dict | None]:
    """The last image in an OpenAI-style request, plus any describe options."""
    options = request.get("describe_options")
    if not isinstance(options, dict):
        options = None
    found = None
    for message in request.get("messages") or []:
        content = message.get("content")
        parts = content if isinstance(content, list) else []
        for part in parts:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "orion_image":
                found = part.get("path") or found
            elif ptype in ("image_url", "input_image"):
                url = part.get("image_url")
                url = url.get("url", "") if isinstance(url, dict) else str(url or "")
                if url:
                    found = url
    return found, options


def serve(host: str = "127.0.0.1", port: int | None = None,
          model_path: str | None = None) -> None:
    """Start the endpoint and serve until interrupted."""
    port = port or paths.MIRAGE_PORT
    bridge = MirageBridge(model_path=model_path)
    bridge.start()
    state = _ServerState(bridge)
    server = ThreadingHTTPServer((host, port), _make_handler(state))
    print(f"[mirage] serving {MODEL_NAME} on http://{host}:{port}/v1 "
          f"(OpenAI-compatible; loopback only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        bridge.stop()


# ── The client side (used by core.llm_client and the openai SDK alike) ───────

class MirageEndpointClient:
    """``describe(image)``-compatible client for the local endpoint.

    Matches the cactus package's Mirage API closely enough that
    ``core.llm_client._mirage_describe`` treats both identically: it returns
    a dict whose ``description`` key carries prose and whose ``raw`` key
    carries the engine's structured JSON.
    """

    def __init__(self, base_url: str, timeout: float = DESCRIBE_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def describe(self, image, **options) -> dict:
        if hasattr(image, "shape") and hasattr(image, "dtype"):
            raise ValueError("numpy arrays are not sent over the endpoint; "
                             "pass a path, bytes or a data URI")
        if isinstance(image, (bytes, bytearray)):
            url = "data:image/png;base64," + base64.b64encode(bytes(image)).decode("ascii")
        elif isinstance(image, str) and image.startswith("data:"):
            url = image                      # a data URI travels as itself
        else:
            url = os.fspath(image)
        body = json.dumps({
            "model": MODEL_NAME,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Describe this image."},
                {"type": "image_url", "image_url": {"url": url}}]}],
            **({"describe_options": options} if options else {}),
        }).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        result = {"description": content}
        raw = payload.get("mirage_raw")
        if isinstance(raw, dict):
            result["raw"] = raw
        return result


def _health_ok(port: int, timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=timeout) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 — anything means "not up"
        return False


_spawn_lock = threading.Lock()
_spawned_pid: int | None = None


def ensure_endpoint(timeout: float = STARTUP_TIMEOUT,
                    port: int | None = None) -> MirageEndpointClient | None:
    """A client for a running — or freshly started — Mirage endpoint.

    Returns ``None`` when the endpoint cannot be brought up (no node, no
    engine artifacts, port taken by something else); the caller then falls
    back to its previous behavior.  Never raises.
    """
    global _spawned_pid
    port = port or paths.MIRAGE_PORT
    base = f"http://127.0.0.1:{port}"
    if _health_ok(port):
        return MirageEndpointClient(base)
    with _spawn_lock:
        if _health_ok(port):          # someone else won the race
            return MirageEndpointClient(base)
        if not shutil.which("node"):
            return None
        if not all(os.path.isfile(p) for p in
                   (paths.MIRAGE_MODEL, paths.MIRAGE_WASM_JS, paths.MIRAGE_WASM)):
            return None
        os.makedirs(paths.LOGS_DIR, exist_ok=True)
        try:
            with open(paths.MIRAGE_LOG, "ab") as log:
                process = subprocess.Popen(
                    [sys.executable, os.path.join(_CORE, "mirage_server.py"),
                     "--port", str(port)],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    start_new_session=True)
            _spawned_pid = process.pid
        except OSError:
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _health_ok(port):
                return MirageEndpointClient(base)
            if process.poll() is not None:
                return None
            time.sleep(0.4)
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve the local Mirage vision model behind an "
                    "OpenAI-compatible /v1 endpoint.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: loopback only)")
    parser.add_argument("--port", type=int, default=None,
                        help=f"port (default: {paths.MIRAGE_PORT})")
    parser.add_argument("--model", default=None,
                        help="path to a .cact vision model "
                             f"(default: {paths.MIRAGE_MODEL})")
    arguments = parser.parse_args()
    serve(host=arguments.host, port=arguments.port, model_path=arguments.model)


if __name__ == "__main__":
    main()
