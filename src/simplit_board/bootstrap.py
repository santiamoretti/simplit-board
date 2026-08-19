"""Bootstrap — install the prerequisites the Java service needs to RUN, before it is ever deployed.

Layering: the Java service provisions the security *tooling* itself (Suricata, OpenVAS, the sandboxed Python)
via its own framework. This step sits one level below that — it makes the box able to run the Java at all:
a JRE, plus the OS-level bits the Java's provisioning relies on (a container runtime for the containerised
OpenVAS, python3 for the sandbox base, and a few essentials).

Design mirrors the Java side: each prerequisite is a small, ordered unit that CHECKS whether it's already
satisfied and only installs if missing. So it's idempotent (re-running is cheap), ordered (@order), and
fail-soft (one missing optional prereq doesn't abort the rest — it's reported). On a Debian/Ubuntu appliance it
installs via apt; on a box where things are already present (e.g. the emulator image) every check just passes.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, List


def _run(cmd: List[str], timeout: float = 600.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _has(binary: str) -> bool:
    return shutil.which(binary) is not None


def _sudo() -> List[str]:
    # Installs need root. Use sudo only if we're not already root and sudo exists. On a non-POSIX host (Windows)
    # there's no euid/sudo concept — return nothing (bootstrap there just reports what's missing, see below).
    try:
        if os.geteuid() == 0:
            return []
    except AttributeError:
        return []
    return ["sudo"] if _has("sudo") else []


# ── OS-agnostic package install ───────────────────────────────────────────────────────────────────────────
# Detect whichever package manager the box has and translate generic tool names to that manager's package
# names, so `bootstrap` runs on any distro/arch (Debian, Fedora/RHEL, Alpine, Arch, openSUSE) and macOS.
_MANAGERS = ("apt-get", "dnf", "yum", "apk", "pacman", "zypper", "brew")

_PKG_NAMES = {
    "essentials": {
        "apt-get": ["ca-certificates", "curl", "procps", "util-linux"],
        "dnf": ["ca-certificates", "curl", "procps-ng", "util-linux"],
        "yum": ["ca-certificates", "curl", "procps-ng", "util-linux"],
        "apk": ["ca-certificates", "curl", "procps"],
        "pacman": ["ca-certificates", "curl", "procps-ng", "util-linux"],
        "zypper": ["ca-certificates", "curl", "procps", "util-linux"],
        "brew": ["curl"],
    },
    "jre": {
        "apt-get": ["openjdk-21-jre-headless"],
        "dnf": ["java-21-openjdk-headless"],
        "yum": ["java-21-openjdk-headless"],
        "apk": ["openjdk21-jre"],
        "pacman": ["jre-openjdk"],
        "zypper": ["java-21-openjdk-headless"],
        "brew": ["openjdk@21"],
    },
    "python3": {
        "apt-get": ["python3", "python3-venv"],
        "dnf": ["python3"],
        "yum": ["python3"],
        "apk": ["python3"],
        "pacman": ["python"],
        "zypper": ["python3"],
        "brew": ["python"],
    },
    "docker": {
        "apt-get": ["docker.io"],
        "dnf": ["docker"],
        "yum": ["docker"],
        "apk": ["docker"],
        "pacman": ["docker"],
        "zypper": ["docker"],
        "brew": ["docker"],
    },
    # Compose v2 is a SEPARATE package from the engine on every distro — `docker.io` ships none of it. The
    # provisioners speak `docker compose …` exclusively, so without this the engine is present, the binary
    # check passes, and every container install fails with "unknown command: docker compose" (measured on the
    # Fibase appliance 2026-08-18, then installed by hand). Compose v1 (`docker-compose`, hyphenated) is NOT a
    # substitute: it does not answer `docker compose`.
    "docker-compose": {
        "apt-get": ["docker-compose-v2"],
        "dnf": ["docker-compose-plugin"],
        "yum": ["docker-compose-plugin"],
        "apk": ["docker-cli-compose"],
        "pacman": ["docker-compose"],
        "zypper": ["docker-compose"],
        "brew": ["docker-compose"],
    },
}


def _pkg_mgr() -> "str | None":
    for mgr in _MANAGERS:
        if _has(mgr):
            return mgr
    return None


def _install_cmds(mgr: str, pkgs: List[str]) -> List[List[str]]:
    if mgr == "apt-get":
        return [["apt-get", "update", "-qq"],
                ["apt-get", "install", "-y", "--no-install-recommends", *pkgs]]
    if mgr in ("dnf", "yum"):
        return [[mgr, "install", "-y", *pkgs]]
    if mgr == "apk":
        return [["apk", "add", "--no-cache", *pkgs]]
    if mgr == "pacman":
        return [["pacman", "-Sy", "--noconfirm", *pkgs]]
    if mgr == "zypper":
        return [["zypper", "--non-interactive", "install", *pkgs]]
    if mgr == "brew":
        return [["brew", "install", *pkgs]]
    return []


# ── Docker, from Docker's own repository ──────────────────────────────────────────────────────────────────
# The distro's `docker.io` gives an engine and NO compose plugin, which is the exact state that let a board
# report a clean bootstrap and then fail every container install. The appliance that has run without trouble
# since it was built (elpais-security-01, verified 2026-08-19) has docker-ce + docker-compose-plugin from
# download.docker.com — so that is what a board gets, and this is the same recipe that installed it, GPG
# fingerprint check included.
_DOCKER_FINGERPRINT = "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
_DOCKER_CONFLICTS = ["docker.io", "docker-doc", "docker-compose", "podman-docker", "containerd", "runc"]
_DOCKER_PACKAGES = ["docker-ce", "docker-ce-cli", "containerd.io",
                    "docker-compose-plugin", "docker-buildx-plugin"]


def _os_release(key: str, default: str = "") -> str:
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return default


def _install_docker_apt() -> None:
    """Add Docker's repository (key fingerprint verified) and install the engine WITH its compose plugin."""
    sudo = _sudo()
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")

    def run(cmd, check=True, timeout=900):
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:4])}… failed: {(r.stderr or r.stdout)[-300:]}")
        return r

    # gnupg is needed to dearmor the key, and is not on every minimal image.
    run([*sudo, "apt-get", "update", "-qq"], check=False)
    run([*sudo, "apt-get", "install", "-y", "--no-install-recommends", "ca-certificates", "curl", "gnupg"])

    # The distro's packages conflict with Docker's own; removing them is what the proven install does.
    for pkg in _DOCKER_CONFLICTS:
        run([*sudo, "apt-get", "remove", "-y", "-qq", pkg], check=False, timeout=300)

    # VERIFY THE KEY before trusting anything signed by it. A repository added with an unverified key is a
    # root-level supply chain wide open, and this runs on customer appliances.
    key_url = "https://download.docker.com/linux/%s/gpg" % (
        "debian" if _os_release("ID") == "debian" else "ubuntu")
    tmp = subprocess.run(["mktemp"], capture_output=True, text=True, timeout=30).stdout.strip()
    try:
        run(["curl", "-4", "-fsSL", key_url, "-o", tmp])
        shown = run(["gpg", "--show-keys", "--with-fingerprint", "--with-colons", tmp]).stdout
        found = ""
        for line in shown.splitlines():
            if line.startswith("fpr:"):
                found = line.split(":")[9]
                break
        if found != _DOCKER_FINGERPRINT:
            raise RuntimeError(
                "Docker's signing key does not match the expected fingerprint — refusing to add the "
                f"repository (expected {_DOCKER_FINGERPRINT}, got {found or 'nothing'})")
        run([*sudo, "mkdir", "-p", "/etc/apt/keyrings"])
        dearmored = subprocess.run(["gpg", "--dearmor"], stdin=open(tmp, "rb"),
                                   capture_output=True, timeout=60)
        keyring = "/etc/apt/keyrings/docker.gpg"
        subprocess.run([*sudo, "tee", keyring], input=dearmored.stdout, capture_output=True, timeout=60)
        run([*sudo, "chmod", "a+r", keyring])
    finally:
        subprocess.run(["rm", "-f", tmp], capture_output=True, timeout=30)

    arch = subprocess.run(["dpkg", "--print-architecture"], capture_output=True, text=True,
                          timeout=30).stdout.strip()
    codename = _os_release("VERSION_CODENAME") or _os_release("UBUNTU_CODENAME")
    if not codename:
        raise RuntimeError("could not determine this system's release codename for Docker's repository")
    distro = "debian" if _os_release("ID") == "debian" else "ubuntu"
    line = (f"deb [arch={arch} signed-by=/etc/apt/keyrings/docker.gpg] "
            f"https://download.docker.com/linux/{distro} {codename} stable\n")
    subprocess.run([*sudo, "tee", "/etc/apt/sources.list.d/docker.list"],
                   input=line.encode(), capture_output=True, timeout=60)

    run([*sudo, "apt-get", "update", "-qq"], check=False)
    run([*sudo, "apt-get", "install", "-y", "--no-install-recommends", *_DOCKER_PACKAGES])


# ── the settings that make an appliance survive being an appliance ────────────────────────────────────────
# Every value here was read off elpais-security-01 on 2026-08-19 — the board that has run 117 days without
# anyone touching it — and each one prevents a failure the others have hit. Written as a MERGE: an installed
# board may carry site-specific keys (that one has its customer's DNS servers), and the point is to add what
# is missing, never to replace someone's file.
_DOCKER_DAEMON_DEFAULTS = {
    # Container logs are unbounded by default. On a board with a small disk and an IDS writing continuously,
    # that is a full filesystem and a dead appliance in weeks.
    "log-driver": "json-file",
    "log-opts": {"max-size": "50m", "max-file": "5"},
    # Keep containers RUNNING while the docker daemon restarts. A monitor that stops inspecting because the
    # daemon was upgraded is a gap nobody is told about.
    "live-restore": True,
    # Docker's default bridge pools (172.17/172.18…) collide with real customer addressing, and when they do
    # the appliance loses the network it was installed to watch. Pick a pool of our own.
    "default-address-pools": [{"base": "172.20.0.0/16", "size": 24}],
    "storage-driver": "overlay2",
    "userland-proxy": False,
}

# The OOM killer must never take the docker daemon (every tool dies with it) or sshd (the way back in).
_OOM_DROPINS = {
    "/etc/systemd/system/docker.service.d/oom-protection.conf":
        "[Service]\n# The tools all live in containers; losing the daemon loses the appliance.\nOOMScoreAdjust=-500\n",
    "/etc/systemd/system/ssh.service.d/oom-protection.conf":
        "[Service]\n# Never a candidate: this is how anyone gets back into a board that is in trouble.\n"
        "OOMScoreAdjust=-1000\nMemoryMax=256M\nTasksMax=100\n",
}


def _docker_daemon_configured() -> bool:
    """True when the daemon config already carries our defaults (extra keys of the site's own are fine)."""
    if not _has("docker"):
        return True   # nothing to configure yet; the runtime prerequisite reports that
    try:
        import json
        with open("/etc/docker/daemon.json", encoding="utf-8") as f:
            cur = json.load(f)
    except (OSError, ValueError):
        return False
    return all(k in cur for k in _DOCKER_DAEMON_DEFAULTS)


def _configure_docker_daemon() -> None:
    import json
    try:
        with open("/etc/docker/daemon.json", encoding="utf-8") as f:
            cur = json.load(f)
    except (OSError, ValueError):
        cur = {}
    merged = dict(cur)
    for k, v in _DOCKER_DAEMON_DEFAULTS.items():
        merged.setdefault(k, v)          # never overwrite what an operator put there
    body = json.dumps(merged, indent=2) + "\n"
    sudo = _sudo()
    subprocess.run([*sudo, "mkdir", "-p", "/etc/docker"], capture_output=True, timeout=60)
    r = subprocess.run([*sudo, "tee", "/etc/docker/daemon.json"],
                       input=body.encode(), capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("could not write /etc/docker/daemon.json")
    # Applied on the daemon's next start; not restarted here because that would stop every running tool
    # underneath whoever is bootstrapping.
    subprocess.run([*sudo, "systemctl", "daemon-reload"], capture_output=True, timeout=60)


def _oom_protected() -> bool:
    return all(os.path.exists(p) for p in _OOM_DROPINS)


def _protect_from_oom() -> None:
    sudo = _sudo()
    for path, body in _OOM_DROPINS.items():
        if os.path.exists(path):
            continue
        subprocess.run([*sudo, "mkdir", "-p", os.path.dirname(path)], capture_output=True, timeout=60)
        subprocess.run([*sudo, "tee", path], input=body.encode(), capture_output=True, timeout=60)
    subprocess.run([*sudo, "systemctl", "daemon-reload"], capture_output=True, timeout=60)


def _operating_user() -> str:
    """Whoever will actually run the board — the invoking account, not root when we got here via sudo."""
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or os.environ.get("LOGNAME") or ""


def _in_docker_group() -> bool:
    """Whether the operating user may talk to the docker socket without sudo.

    The board service runs unprivileged and shells out to `docker compose`; without this it gets permission
    denied on /var/run/docker.sock and every tool install fails for a reason that looks nothing like a
    missing group. The appliance that works has its user in this group.
    """
    user = _operating_user()
    if not user:
        return True   # no user to add (a container, a root-only image) — not something to fail on
    try:
        out = _run(["id", "-nG", user], timeout=30).stdout
        return "docker" in out.split()
    except Exception:  # noqa: BLE001
        return True


def _add_to_docker_group() -> None:
    user = _operating_user()
    if not user:
        return
    r = subprocess.run([*_sudo(), "usermod", "-aG", "docker", user], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"could not add {user} to the docker group: {(r.stderr or '')[-200:]}")


def _install(generic: str) -> None:
    mgr = _pkg_mgr()
    if generic == "docker" and mgr == "apt-get":
        _install_docker_apt()
        return
    if mgr is None:
        raise RuntimeError(
            "no supported package manager (apt/dnf/yum/apk/pacman/zypper/brew) — install '%s' manually" % generic)
    pkgs = _PKG_NAMES.get(generic, {}).get(mgr)
    if not pkgs:
        raise RuntimeError(f"don't know how to install '{generic}' with {mgr} — install it manually")
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    prefix = [] if mgr == "brew" else _sudo()   # Homebrew refuses to run as root
    for cmd in _install_cmds(mgr, pkgs):
        r = subprocess.run([*prefix, *cmd], env=env, timeout=900, capture_output=True, text=True)
        # An index-refresh step failing (apt update) is non-fatal; the actual install step is.
        if r.returncode != 0 and not (cmd[:1] == ["apt-get"] and "update" in cmd):
            raise RuntimeError(f"{mgr} install of '{generic}' failed: {(r.stderr or r.stdout)[-300:]}")


def _state_dir() -> str:
    return os.environ.get("SIMPLIT_STATE_DIR", "/var/lib/simplit")


def _state_dir_ready() -> bool:
    d = _state_dir()
    return os.path.isdir(d) and os.access(d, os.W_OK)


def _make_state_dir() -> None:
    # Create the agent's state dir and hand it to the OPERATING user, so `register`/`up` run WITHOUT sudo and
    # the device identity/credential never end up owned by root. The user is whoever invoked us (SUDO_USER when
    # run via sudo, else the current user). Mirrors what operators used to do by hand.
    d = _state_dir()
    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    cmds = [[*_sudo(), "mkdir", "-p", d]]
    if user:
        # owner only (no group) — group names differ across OSes (staff on macOS, the user's group on Linux).
        cmds.append([*_sudo(), "chown", "-R", user, d])
    cmds.append([*_sudo(), "chmod", "750", d])
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed: {(r.stderr or '')[-200:]}")


def _compose_ok() -> bool:
    """Whether `docker compose` — the SUBCOMMAND, not the binary — actually answers.

    The old check was `shutil.which("docker")`, which is satisfied by an engine with no compose plugin: the
    exact state that let a board report a clean bootstrap and then fail every container install. Ask the
    thing we are going to call.
    """
    if not _has("docker"):
        return False
    try:
        return _run(["docker", "compose", "version"], timeout=60).returncode == 0
    except Exception:   # noqa: BLE001 — a daemon that is down/absent is simply "not satisfied"
        return False


def _java_ok(min_major: int = 21) -> bool:
    if not _has("java"):
        return False
    try:
        out = _run(["java", "-version"], timeout=30).stderr or ""
        # e.g. openjdk version "21.0.3" — take the first integer
        for tok in out.replace('"', " ").split():
            if tok[:1].isdigit():
                return int(tok.split(".")[0]) >= min_major
    except Exception:
        return False
    return False


@dataclass
class Prerequisite:
    name: str
    order: int
    check: Callable[[], bool]
    install: Callable[[], None]
    required: bool = True  # a non-required prereq that fails is a warning, not a stop


@dataclass
class Result:
    name: str
    status: str          # "already" | "installed" | "skipped" | "failed"
    detail: str = ""


# ── the prerequisite set (ordered) ──────────────────────────────────────────────────────────────────
def _essentials_present() -> bool:
    return all(_has(b) for b in ("curl", "ps"))


PREREQS: List[Prerequisite] = [
    Prerequisite("os-essentials", 10, _essentials_present,
                 lambda: _install("essentials")),
    # The JRE — the whole point: the Java service can't run without it. Java 21 (the board is built for 21).
    Prerequisite("jre-21", 20, _java_ok,
                 lambda: _install("jre")),
    # python3 — the sandbox base the Java's Python provisioner builds on.
    Prerequisite("python3", 30, lambda: _has("python3"),
                 lambda: _install("python3")),
    # A container runtime — only the containerised OpenVAS needs it; not fatal if absent (Suricata/Python are
    # in-process). Marked not-required so a board that never runs OpenVAS still bootstraps cleanly.
    Prerequisite("container-runtime", 40, lambda: _has("docker"),
                 lambda: _install("docker"), required=False),
    # …and its compose plugin, which is what the provisioners actually invoke. Separate unit from the engine
    # because the engine can be present without it — the state that broke a customer appliance. On apt this
    # normally passes already: Docker's own repository ships the plugin with the engine above.
    Prerequisite("container-compose", 45, _compose_ok,
                 lambda: _install("docker-compose"), required=False),
    # Talking to the docker socket without sudo. The board runs unprivileged, so without this every
    # container command it issues fails with a permission error that reads like anything but a missing group.
    # Takes effect on the next login/boot, which is fine: the board service is launched after this.
    Prerequisite("docker-group", 46, _in_docker_group, _add_to_docker_group, required=False),
    # Bounded container logs, containers that survive a daemon restart, and an address pool that does not
    # collide with the customer's own network — read off the appliance that has run untouched for months.
    Prerequisite("docker-daemon-config", 47, _docker_daemon_configured, _configure_docker_daemon,
                 required=False),
    # Keep the OOM killer away from the docker daemon and from sshd.
    Prerequisite("oom-protection", 48, _oom_protected, _protect_from_oom, required=False),
    # The agent's state dir (identity + credential). Created and handed to the operating user so `register`/`up`
    # run without sudo and the identity is never owned by root. Not required — a custom SIMPLIT_STATE_DIR that
    # is already writable just passes the check.
    Prerequisite("state-dir", 50, _state_dir_ready, _make_state_dir, required=False),
]


class Bootstrapper:
    def __init__(self, prereqs: List[Prerequisite] | None = None, log=print):
        self.prereqs = sorted(prereqs or PREREQS, key=lambda p: p.order)
        self.log = log

    def run(self) -> List[Result]:
        results: List[Result] = []
        for p in self.prereqs:
            try:
                if p.check():
                    self.log(f"[bootstrap] {p.name}: already present")
                    results.append(Result(p.name, "already"))
                    continue
                self.log(f"[bootstrap] {p.name}: installing…")
                p.install()
                if p.check():
                    self.log(f"[bootstrap] {p.name}: installed")
                    results.append(Result(p.name, "installed"))
                else:
                    raise RuntimeError("still not satisfied after install")
            except Exception as e:  # noqa: BLE001
                status = "failed" if p.required else "skipped"
                self.log(f"[bootstrap] {p.name}: {status} — {e}")
                results.append(Result(p.name, status, str(e)[:200]))
        return results

    @staticmethod
    def ok(results: List[Result]) -> bool:
        """True if every REQUIRED prerequisite ended up satisfied."""
        return all(r.status in ("already", "installed") for r in results
                   if r.status != "skipped")


def java_ready() -> bool:
    """Cheap preflight the deploy path calls before launching the jar."""
    return _java_ok()


def compose_ready() -> bool:
    """Whether the containerised tools (scanner, monitor) can be installed at all."""
    return _compose_ok()
