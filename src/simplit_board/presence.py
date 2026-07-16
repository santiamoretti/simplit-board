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

    def stop(self) -> None:
        self._stop = True

    def run_forever(self) -> None:
        backoff = 1.0
        while not self._stop:
            try:
                url = f"{self._ws_url}?token={self._tokens.current()}"
                self._log(f"[presence] connecting as {self._device_id} …")
                ws = websocket.create_connection(url, timeout=100, enable_multithread=True)
                self._log("[presence] connected — waiting for pushes")
                backoff = 1.0
                self._pump(ws)
            except Exception as e:
                if self._stop:
                    break
                self._log(f"[presence] disconnected: {e} — reconnecting in {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _pump(self, ws) -> None:
        while not self._stop:
            raw = ws.recv()
            if raw is None or raw == "":
                continue
            try:
                frame = json.loads(raw)
            except Exception:
                continue
            t = frame.get("type")
            if t == "deviceJob":
                self._on_device_job(ws, frame)
            elif t == "deployStart":
                self._on_deploy_start(frame)
            elif t == "deployChunk":
                self._on_deploy_chunk(frame)
            elif t == "deployEnd":
                if self._on_deploy_end(ws, frame):
                    return


    def _on_device_job(self, ws, frame: dict) -> None:
        request_id = frame.get("requestId")
        self._log(f"[presence] deviceJob {request_id} received")
        try:
            result = self._handler(frame) if self._handler else {
                "boardId": self._device_id, "status": "rejected", "detail": "no job handler"}
        except Exception as e:
            result = {"boardId": self._device_id, "status": "failed", "detail": str(e)[:400]}
        ws.send(json.dumps({"type": "deviceJobResult", "requestId": request_id, "result": result}))
        self._log(f"[presence] deviceJobResult {request_id} -> {result.get('status')}: {result.get('detail','')}")
        if self._yield_after_deploy and result.get("status") == "deployed":
            self._yield(ws, "board service installed")


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

    def _on_deploy_end(self, ws, frame: dict) -> bool:
        deploy_id = frame.get("deployId")
        st = self._pending.pop(deploy_id, None)
        result = self._assemble_and_deploy(st)
        ws.send(json.dumps({"type": "deployResult", "requestId": deploy_id, "result": result}))
        self._log(f"[deploy] {deploy_id} -> {result.get('status')}: {result.get('detail','')}")
        if result.get("status") == "deployed" and self._yield_after_deploy:
            self._yield(ws, "board service installed from streamed bytes")
            return True
        return False

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

    def _yield(self, ws, why: str) -> None:
        self._log(f"[presence] {why} — yielding the presence session to it")
        self._stop = True
        try:
            ws.close()
        except Exception:
            pass
