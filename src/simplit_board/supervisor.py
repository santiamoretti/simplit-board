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
import tempfile
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
                 register_url: Optional[str] = None, delivery_pubkey: Optional[str] = None,
                 control_pubkey: Optional[str] = None):
        self.jar_dir = jar_dir
        self.java_bin = java_bin
        self.baked_jar = baked_jar or os.environ.get("SIMPLIT_BOARD_JAR")
        # The deployed Java service IS the device on the presence data channel (reports/findings). It needs the
        # device's identity + the live cloud URLs, or it can't mint a token / connect / be queried.
        self.device_id = device_id
        self.device_secret = device_secret
        self.token_url = token_url
        self.presence_url = presence_url
        # The gateway /api/register URL — set so the board announces itself + heartbeats -> shows ONLINE while up.
        self.register_url = register_url
        # The build/delivery trust anchor the agent resolved (pinned in-band at enrollment). We hand this to the
        # Java board so it verifies its OWN future pushes against the SAME key — the agent only does the first one.
        self.delivery_pubkey = delivery_pubkey
        # Control's key, resolved the same way. The board verifies every control job against it; with none, it
        # rejects them all, so this must reach the JVM env or the Updates tab cannot touch this board.
        self.control_pubkey = control_pubkey
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
            os.replace(nxt, self.app_jar)  # atomic
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
        # A JRE must be present, or the artifact can never run. Fail with a clear, actionable message rather
        # than a raw "java: not found" — the operator just needs to run `simplit-board bootstrap`.
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
        os.replace(nxt, self.app_jar)  # atomic
        return self._launch(version, [], grace_seconds)

    def _launch(self, version: str, extra: list[str], grace_seconds: float = 35.0) -> str:
        self._stop()

        # Run the board service AS the device: it takes over the presence data channel (reports/findings) once
        # up, so it must have the device identity + the live cloud URLs. Registration/provisioning stay off (the
        # agent already registered; tools aren't provisioned in the emulator). Store at a writable path.
        env = dict(os.environ)
        env.setdefault("SIMPLIT_REGISTER_ENABLED", "false")
        env.setdefault("SIMPLIT_PROVISION_ENABLED", "false")
        env["SIMPLIT_PRESENCE_ENABLED"] = "true"          # the Java owns the report/query channel
        env.setdefault("SIMPLIT_STORE_PATH", str(self.jar_dir / "board.db"))
        env.setdefault("SIMPLIT_REPORT_EXEC", "python3 /opt/simplit/pysandbox/worker.py")
        env.setdefault("SIMPLIT_DEVICE_IDENTITY_PATH", str(self.jar_dir.parent / "device-identity.json"))
        env.setdefault("SIMPLIT_SIGNAL_IDENTITY_PATH", str(self.jar_dir.parent / "device-signal.json"))
        # Hand the board its ENROLLED Ed25519 private key so it can SIGN its Signal bundle with the device's
        # canonical identity (the FE verifies that signature against the gateway-served canonical identity, so a
        # malicious relay can't impersonate the device). The key is the raw seed the agent minted at first boot
        # (state_dir/device.key); the board wraps it into PKCS#8 itself. Best-effort — absent ⇒ unsigned bundle.
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
        # The Java verifies control's pushes itself — map our control pubkey to the name it reads, or it rejects
        # every job ("no control public key configured"). The anchor pinned at enrollment wins over the ambient
        # environment: relying on the env alone is what left boards silently unable to accept any control job.
        pub = self.control_pubkey or os.environ.get("SIMPLIT_CONTROL_PUBKEY") \
            or os.environ.get("SIMPLIT_CONTROL_PUBLIC_KEY")
        if pub:
            env["SIMPLIT_CONTROL_PUBLIC_KEY"] = pub
        else:
            log_msg = "[warn] no control public key — this board will reject control jobs (updates, commands)"
            print(log_msg, flush=True)
        # The board service OWNS presence and receives its OWN future updates. Give it the delivery trust key,
        # its own jar path (to overwrite on self-update), the java binary, and the signal flag.
        env["SIMPLIT_SIGNAL_ENABLED"] = "true"
        env["SIMPLIT_SELF_JAR"] = str(self.app_jar)
        env["SIMPLIT_JAVA_BIN"] = self.java_bin
        env.setdefault("SIMPLIT_ORG_ID", os.environ.get("SIMPLIT_ORG", "simplit"))
        # Hand the board its trust anchor so IT verifies future pushes (it owns presence after this install). Use
        # the anchor the agent resolved (pinned in-band at enrollment) FIRST — not just the environment — or a
        # board installed via the pinned key would boot with no key and reject every subsequent push. It IS the
        # build key, so set it as the board's primary build key; also fill the legacy delivery slot for dual-trust.
        dpub = self.delivery_pubkey or os.environ.get("SIMPLIT_DELIVERY_PUBKEY") \
            or os.environ.get("SIMPLIT_DELIVERY_PUBLIC_KEY")
        if dpub:
            env["SIMPLIT_BUILD_PUBLIC_KEY"] = dpub
            env["SIMPLIT_DELIVERY_PUBLIC_KEY"] = dpub
        # Announce to the gateway registry (+ heartbeat) so operators see the board ONLINE the whole time it's up.
        # DIRECT assignment (not setdefault): an earlier line defaults ENABLED=false, so setdefault would no-op —
        # providing a register_url is the agent's explicit intent to register, so it must win.
        if self.register_url:
            env["SIMPLIT_REGISTER_ENABLED"] = "true"
            env["SIMPLIT_REGISTER_URL"] = self.register_url

        log = open(self.log_file, "wb")
        # Run the board under a restart loop, in its OWN session (start_new_session) so it OUTLIVES this
        # ephemeral agent AND can self-restart into a new version on a re-push: the board exits 88 after
        # writing its new jar, and this loop relaunches it. On a real board this loop is systemd Restart=.
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
        # Survive POWER LOSS. The detached wrapper above outlives this agent, but nothing outlives a
        # reboot: measured on a fresh board, a power cycle brought back a registered, presence-connected
        # agent and NO board service — an appliance that looks healthy and scans nothing. So the running
        # jar is also handed to systemd, which owns it from the next boot on.
        self._install_boot_unit(env)

        # Health: succeed only on Spring's real "Started …Application" line; fail fast on a run failure or exit.
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

        # No explicit "Started" but still alive past the grace window — treat as running (headless workloads).
        if self.proc.poll() is None:
            self.version = version
            return f"deployed {version} (pid {self.proc.pid}, running)"
        raise DeployError("java did not report started within the grace window")

    BOOT_UNIT = "simplit-board-service.service"

    def _install_boot_unit(self, env: dict) -> None:
        """Register the board service with systemd so a power cut cannot leave the appliance empty.

        Best-effort by design: a board that cannot write a unit (no systemd, no sudo) still has the
        running service the deploy just started — losing boot persistence must never fail a deploy that
        otherwise worked. The unit runs the same wrapper the agent runs, with the same environment, so
        there is one launch path and rc=88 self-updates keep working.
        """
        try:
            wrapper = self.jar_dir / "run-board.sh"
            keep = {k: v for k, v in env.items() if k.startswith("SIMPLIT_")}
            lines = "\n".join(f"Environment={k}={v}" for k, v in sorted(keep.items()))
            unit = (
                "[Unit]\n"
                "Description=Simplit board service\n"
                "After=network-online.target\n"
                "Wants=network-online.target\n\n"
                "[Service]\n"
                "Type=simple\n"
                "User=root\n"
                f"{lines}\n"
                f"ExecStart=/bin/bash {wrapper}\n"
                "Restart=always\n"
                "RestartSec=10\n\n"
                "[Install]\n"
                "WantedBy=multi-user.target\n"
            )
            path = f"/etc/systemd/system/{self.BOOT_UNIT}"
            sudo = [] if os.geteuid() == 0 else ["sudo", "-n"]
            with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False) as tf:
                tf.write(unit)
                tmp = tf.name
            try:
                subprocess.run([*sudo, "cp", tmp, path], check=True, timeout=30)
            finally:
                os.unlink(tmp)
            subprocess.run([*sudo, "systemctl", "daemon-reload"], check=True, timeout=30)
            # ENABLE only — starting it now would race the instance this deploy just launched. systemd
            # takes over at the next boot, which is exactly the case that was broken.
            subprocess.run([*sudo, "systemctl", "enable", self.BOOT_UNIT], check=True, timeout=30)
        except Exception as e:  # noqa: BLE001 - boot persistence is best-effort, never fatal to a deploy
            print(f"[deploy] warning: could not register the board service for boot ({e}) — "
                  "it is running now but will NOT come back after a power cut")

    def boot_unit_active(self) -> bool:
        """Is the board service currently running under systemd? Used by the agent to stand down rather
        than contend for the device's single presence session. False whenever we cannot tell."""
        try:
            r = subprocess.run(["systemctl", "is-active", self.BOOT_UNIT],
                               capture_output=True, text=True, timeout=10)
            return r.stdout.strip() == "active"
        except Exception:  # noqa: BLE001 - no systemd / not permitted ⇒ behave as before
            return False

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
