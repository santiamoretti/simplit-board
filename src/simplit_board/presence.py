"""Presence relay client — the bootstrap + delivery channel.

Connects to the presence relay as this device (JWT in the ``?token=`` query — a header-based handshake 401s),
then receives frames. Two things arrive here:

  * ``deviceJob`` — a control-signed job (legacy reference deploy / os-command). Verified + applied by ``handler``.
  * ``deployStart`` / ``deployChunk`` / ``deployEnd`` — the DELIVERY service streaming an artifact's BYTES down
    to this board. We authenticate the manifest (Ed25519 over the artifact's SHA-256), reassemble the chunks,
    re-check the hash, and run exactly what we received — fetching nothing, touching no registry.

The socket is kept alive with application-level WebSocket ping/pong: the relay lives behind an ingress that
drops IDLE WebSocket connections, so without pings a session goes half-open — the agent stays blocked on recv
printing "waiting for pushes" while the relay has already marked the device OFFLINE (so pushes fail). A ping
every ``ping_interval`` keeps the session up; a missed pong within ``ping_timeout`` closes it and we reconnect.

With ``yield_after_deploy=True`` the client stops the instant an artifact is installed: it replies success,
closes its socket, and returns — yielding the one-per-device presence session to the board service the deploy
just started (which reconnects as the same device and owns the channel from then on). The relay is last-writer-
wins and its close is session-guarded, so this handoff is race-free.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Callable, Optional

import websocket

from . import verify


JobHandler = Callable[[dict], dict]
ArtifactHandler = Callable[[dict, bytes], dict]

PING_INTERVAL = 20
PING_TIMEOUT = 10
BACKOFF_START = 0.5
BACKOFF_MAX = 5.0
HEALTHY_SECS = 20.0


class PresenceClient:
    def __init__(self, ws_url: str, token_provider, device_id: str, handler: Optional[JobHandler] = None,
                 log=print, yield_after_deploy: bool = False,
                 artifact_handler: Optional[ArtifactHandler] = None, delivery_key=None):
        self._ws_url = ws_url
        self._tokens = token_provider
        self._device_id = device_id
        self._handler = handler
        self._log = log
        self._yield_after_deploy = yield_after_deploy
        self._artifact_handler = artifact_handler
        self._delivery_key = delivery_key
        self._pending: dict[str, dict] = {}
        self._stop = False
        self._done = False
        self._app: Optional[websocket.WebSocketApp] = None

    def stop(self) -> None:
        self._stop = True
        if self._app is not None:
            try:
                self._app.close()
            except Exception:
                pass

    def run_forever(self) -> None:
        backoff = BACKOFF_START
        while not self._stop and not self._done:
            url = f"{self._ws_url}?token={self._tokens.current()}"
            self._log(f"[presence] connecting as {self._device_id} …")
            self._app = websocket.WebSocketApp(
                url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            started = time.monotonic()
            try:
                self._app.run_forever(ping_interval=PING_INTERVAL, ping_timeout=PING_TIMEOUT,
                                      skip_utf8_validation=True)
            except Exception as e:
                self._log(f"[presence] error: {e}")
            if self._stop or self._done:
                break
            if time.monotonic() - started >= HEALTHY_SECS:
                backoff = BACKOFF_START
            self._log(f"[presence] disconnected — reconnecting in {backoff:.1f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

    def _on_open(self, _app) -> None:
        self._log("[presence] connected — waiting for pushes")

    def _on_error(self, _app, err) -> None:
        self._log(f"[presence] connection error: {err}")

    def _on_close(self, _app, status_code, msg) -> None:
        pass

    def _on_message(self, _app, raw) -> None:
        if not raw:
            return
        try:
            frame = json.loads(raw)
        except Exception:
            return
        t = frame.get("type")
        if t == "deviceJob":
            self._on_device_job(frame)
        elif t == "deployStart":
            self._on_deploy_start(frame)
        elif t == "deployChunk":
            self._on_deploy_chunk(frame)
        elif t == "deployEnd":
            self._on_deploy_end(frame)

    def _send(self, obj: dict) -> None:
        if self._app is not None:
            self._app.send(json.dumps(obj))

    def _on_device_job(self, frame: dict) -> None:
        request_id = frame.get("requestId")
        self._log(f"[presence] deviceJob {request_id} received")
        try:
            result = self._handler(frame) if self._handler else {
                "boardId": self._device_id, "status": "rejected", "detail": "no job handler"}
        except Exception as e:
            result = {"boardId": self._device_id, "status": "failed", "detail": str(e)[:400]}
        self._send({"type": "deviceJobResult", "requestId": request_id, "result": result})
        self._log(f"[presence] deviceJobResult {request_id} -> {result.get('status')}: {result.get('detail','')}")
        if self._yield_after_deploy and result.get("status") == "deployed":
            self._yield("board service installed")

    def _on_deploy_start(self, frame: dict) -> None:
        deploy_id = frame.get("deployId")
        manifest = {k: frame.get(k) for k in
                    ("service", "version", "size", "sha256", "chunkCount", "chunkBytes", "sig")}
        authentic = False
        if self._delivery_key is not None and manifest.get("sha256") and manifest.get("sig"):
            try:
                sha_bytes = base64.b64decode(manifest["sha256"])
                authentic = verify.verify_signature(self._delivery_key, sha_bytes, manifest["sig"])
            except Exception:
                authentic = False
        self._pending[deploy_id] = {"manifest": manifest, "chunks": {}, "authentic": authentic}
        self._log(f"[deploy] {deploy_id} start: {manifest.get('service')} v={manifest.get('version')} "
                  f"{manifest.get('size')}B in {manifest.get('chunkCount')} chunks — signature "
                  f"{'VERIFIED' if authentic else 'REJECTED'}")

    def _on_deploy_chunk(self, frame: dict) -> None:
        st = self._pending.get(frame.get("deployId"))
        if st is None:
            return
        try:
            st["chunks"][int(frame.get("seq"))] = base64.b64decode(frame.get("data") or "")
        except Exception:
            pass

    def _on_deploy_end(self, frame: dict) -> None:
        deploy_id = frame.get("deployId")
        st = self._pending.pop(deploy_id, None)
        result = self._assemble_and_deploy(st)
        self._send({"type": "deployResult", "requestId": deploy_id, "result": result})
        self._log(f"[deploy] {deploy_id} -> {result.get('status')}: {result.get('detail','')}")
        if result.get("status") == "deployed" and self._yield_after_deploy:
            self._yield("board service installed from streamed bytes")

    def _assemble_and_deploy(self, st: Optional[dict]) -> dict:
        if st is None:
            return {"boardId": self._device_id, "status": "failed", "detail": "unknown deploy"}
        m = st["manifest"]
        if not st["authentic"]:
            return {"boardId": self._device_id, "status": "rejected", "detail": "bad delivery signature"}
        try:
            n = int(m["chunkCount"])
        except Exception:
            return {"boardId": self._device_id, "status": "failed", "detail": "bad manifest"}
        missing = [i for i in range(n) if i not in st["chunks"]]
        if missing:
            return {"boardId": self._device_id, "status": "failed", "detail": f"missing {len(missing)} chunk(s)"}
        data = b"".join(st["chunks"][i] for i in range(n))
        if len(data) != int(m["size"]):
            return {"boardId": self._device_id, "status": "failed",
                    "detail": f"size mismatch: got {len(data)}, expected {m['size']}"}
        actual = base64.b64encode(hashlib.sha256(data).digest()).decode()
        if actual != m["sha256"]:
            return {"boardId": self._device_id, "status": "failed", "detail": "sha256 mismatch"}
        if self._artifact_handler is None:
            return {"boardId": self._device_id, "status": "failed", "detail": "no artifact handler"}
        try:
            return self._artifact_handler(m, data)
        except Exception as e:
            return {"boardId": self._device_id, "status": "failed", "detail": f"apply error: {str(e)[:300]}"}

    def _yield(self, why: str) -> None:
        self._log(f"[presence] {why} — yielding the presence session to it")
        self._done = True
        self._stop = True
        if self._app is not None:
            try:
                self._app.close()
            except Exception:
                pass
