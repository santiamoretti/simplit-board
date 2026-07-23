"""Deploy + supervise the Java service.

This is the "receives the update and deploys it" half. A verified job carries the artifact reference (a jar URL
in ``imageRef``, or a baked jar if none). We fetch it if needed, atomically swap it into place, launch it with
``java -jar``, and health-check that it actually comes up (a ``Started`` line, or simply staying alive past a
grace window). On failure we roll back to the previous jar. The Java process is the thing that runs the actual
security tooling; the agent only owns getting the right, verified jar running.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import requests


class DeployError(RuntimeError):
    pass


class Supervisor:
    def __init__(self, jar_dir: Path, java_bin: str = "java", baked_jar: Optional[str] = None,
                 device_id: Optional[str] = None, device_secret: Optional[str] = None,
                 token_url: Optional[str] = None, presence_url: Optional[str] = None,
                 register_url: Optional[str] = None, delivery_pubkey: Optional[str] = None):
        self.jar_dir = jar_dir
        self.java_bin = java_bin
        self.baked_jar = baked_jar or os.environ.get("SIMPLIT_BOARD_JAR")
        self.device_id = device_id
        self.device_secret = device_secret
        self.token_url = token_url
        self.presence_url = presence_url
        self.register_url = register_url
        self.delivery_pubkey = delivery_pubkey
        self.jar_dir.mkdir(parents=True, exist_ok=True)
        self.proc: Optional[subprocess.Popen] = None
        self.version: Optional[str] = None

    @property
    def app_jar(self) -> Path:
        return self.jar_dir / "app.jar"

    @property
    def log_file(self) -> Path:
        return self.jar_dir / "service.log"

    def _resolve_jar(self, image_ref: str) -> None:
        """Populate app.jar: download image_ref if it's a URL, else copy the baked jar."""
        if image_ref.startswith("http://") or image_ref.startswith("https://"):
            nxt = self.jar_dir / "next.jar"
            with requests.get(image_ref, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(nxt, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        f.write(chunk)
            if self.app_jar.exists():
                shutil.copy2(self.app_jar, self.jar_dir / "app.jar.previous")
            os.replace(nxt, self.app_jar)
        elif self.baked_jar and Path(self.baked_jar).exists():
            if self.app_jar.resolve() != Path(self.baked_jar).resolve():
                shutil.copy2(self.baked_jar, self.app_jar)
        elif not self.app_jar.exists():
            raise DeployError(f"no jar to deploy: imageRef '{image_ref}' is not a URL and no baked jar is set")

    def _stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def _preflight(self) -> None:
        from . import bootstrap
        if not bootstrap.java_ready():
            raise DeployError("no Java runtime — run `simplit-board bootstrap` to install prerequisites first")

    def deploy(self, job: dict[str, Any], grace_seconds: float = 35.0) -> str:
        """Deploy from a job reference (legacy: fetch imageRef URL or use a baked jar)."""
        self._preflight()
        image_ref = job.get("imageRef") or job.get("image") or ""
        version = image_ref or job.get("componentId") or "unknown"
        self._resolve_jar(image_ref)
        return self._launch(version, job.get("javaArgs") or [], grace_seconds)

    def deploy_bytes(self, data: bytes, version: str = "pushed", grace_seconds: float = 35.0) -> str:
        """Deploy an artifact received AS BYTES over the relay — write it into place and run it, fetching
        nothing. This is the content-blind delivery path: the caller has already verified the signature + hash.
        """
        self._preflight()
        self.jar_dir.mkdir(parents=True, exist_ok=True)
        nxt = self.jar_dir / "next.jar"
        with open(nxt, "wb") as f:
            f.write(data)
        if self.app_jar.exists():
            shutil.copy2(self.app_jar, self.jar_dir / "app.jar.previous")
        os.replace(nxt, self.app_jar)
        return self._launch(version, [], grace_seconds)

    def _launch(self, version: str, extra: list[str], grace_seconds: float = 35.0) -> str:
        self._stop()

        env = dict(os.environ)
        env.setdefault("SIMPLIT_REGISTER_ENABLED", "false")
        env.setdefault("SIMPLIT_PROVISION_ENABLED", "false")
        env["SIMPLIT_PRESENCE_ENABLED"] = "true"
        env.setdefault("SIMPLIT_STORE_PATH", str(self.jar_dir / "board.db"))
        env.setdefault("SIMPLIT_REPORT_EXEC", "python3 /opt/simplit/pysandbox/worker.py")
        env.setdefault("SIMPLIT_DEVICE_IDENTITY_PATH", str(self.jar_dir.parent / "device-identity.json"))
        env.setdefault("SIMPLIT_SIGNAL_IDENTITY_PATH", str(self.jar_dir.parent / "device-signal.json"))
        key_file = self.jar_dir.parent / "device.key"
        if "SIMPLIT_DEVICE_SIGNING_KEY" not in env and key_file.exists():
            try:
                env["SIMPLIT_DEVICE_SIGNING_KEY"] = key_file.read_text().strip()
            except OSError:
                pass
        if self.device_id:
            env["SIMPLIT_DEVICE_ID"] = self.device_id
        if self.device_secret:
            env["SIMPLIT_DEVICE_SECRET"] = self.device_secret
        if self.token_url:
            env["SIMPLIT_AUTH_TOKEN_URL"] = self.token_url
        if self.presence_url:
            env["SIMPLIT_PRESENCE_URL"] = self.presence_url
        pub = os.environ.get("SIMPLIT_CONTROL_PUBKEY") or os.environ.get("SIMPLIT_CONTROL_PUBLIC_KEY")
        if pub:
            env.setdefault("SIMPLIT_CONTROL_PUBLIC_KEY", pub)
        env["SIMPLIT_SIGNAL_ENABLED"] = "true"
        env["SIMPLIT_SELF_JAR"] = str(self.app_jar)
        env["SIMPLIT_JAVA_BIN"] = self.java_bin
        env.setdefault("SIMPLIT_ORG_ID", os.environ.get("SIMPLIT_ORG", "simplit"))
        dpub = self.delivery_pubkey or os.environ.get("SIMPLIT_DELIVERY_PUBKEY") \
            or os.environ.get("SIMPLIT_DELIVERY_PUBLIC_KEY")
        if dpub:
            env["SIMPLIT_BUILD_PUBLIC_KEY"] = dpub
            env["SIMPLIT_DELIVERY_PUBLIC_KEY"] = dpub
        if self.register_url:
            env["SIMPLIT_REGISTER_ENABLED"] = "true"
            env["SIMPLIT_REGISTER_URL"] = self.register_url

        log = open(self.log_file, "wb")
        wrapper = self.jar_dir / "run-board.sh"
        wrapper.write_text(
            "#!/bin/bash\n"
            "while :; do\n"
            '  "${SIMPLIT_JAVA_BIN:-java}" -jar "$SIMPLIT_SELF_JAR"\n'
            "  rc=$?\n"
            '  if [ "$rc" = "88" ]; then echo "[wrapper] update installed (rc=88) — relaunching new jar"; continue; fi\n'
            '  echo "[wrapper] board exited rc=$rc — stopping"; exit "$rc"\n'
            "done\n"
        )
        self.proc = subprocess.Popen(
            ["bash", str(wrapper)],
            stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True,
        )

        deadline = time.time() + grace_seconds
        while time.time() < deadline:
            if self.proc.poll() is not None:
                tail = _tail(self.log_file)
                self._rollback()
                raise DeployError(f"java exited early (rc={self.proc.returncode}). last log:\n{tail}")
            if _log_has(self.log_file, b"Application run failed") or _log_has(self.log_file, b"APPLICATION FAILED TO START"):
                tail = _tail(self.log_file)
                self._stop()
                self._rollback()
                raise DeployError(f"java failed to start. last log:\n{tail}")
            if _log_has(self.log_file, b"Started ") and _log_has(self.log_file, b"Application in "):
                self.version = version
                return f"deployed {version} (pid {self.proc.pid}, started)"
            time.sleep(1.0)

        if self.proc.poll() is None:
            self.version = version
            return f"deployed {version} (pid {self.proc.pid}, running)"
        raise DeployError("java did not report started within the grace window")

    def _rollback(self) -> None:
        prev = self.jar_dir / "app.jar.previous"
        if prev.exists():
            shutil.copy2(prev, self.app_jar)

    def status(self) -> dict[str, Any]:
        alive = bool(self.proc and self.proc.poll() is None)
        return {"running": alive, "version": self.version, "pid": self.proc.pid if self.proc else None}


def _tail(path: Path, n: int = 40) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except Exception:
        return ""


def _log_has(path: Path, needle: bytes) -> bool:
    try:
        return needle in path.read_bytes()
    except Exception:
        return False
