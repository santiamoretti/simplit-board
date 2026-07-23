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
    try:
        if os.geteuid() == 0:
            return []
    except AttributeError:
        return []
    return ["sudo"] if _has("sudo") else []


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


def _install(generic: str) -> None:
    mgr = _pkg_mgr()
    if mgr is None:
        raise RuntimeError(
            "no supported package manager (apt/dnf/yum/apk/pacman/zypper/brew) — install '%s' manually" % generic)
    pkgs = _PKG_NAMES.get(generic, {}).get(mgr)
    if not pkgs:
        raise RuntimeError(f"don't know how to install '{generic}' with {mgr} — install it manually")
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    prefix = [] if mgr == "brew" else _sudo()
    for cmd in _install_cmds(mgr, pkgs):
        r = subprocess.run([*prefix, *cmd], env=env, timeout=900, capture_output=True, text=True)
        if r.returncode != 0 and not (cmd[:1] == ["apt-get"] and "update" in cmd):
            raise RuntimeError(f"{mgr} install of '{generic}' failed: {(r.stderr or r.stdout)[-300:]}")


def _state_dir() -> str:
    return os.environ.get("SIMPLIT_STATE_DIR", "/var/lib/simplit")


def _state_dir_ready() -> bool:
    d = _state_dir()
    return os.path.isdir(d) and os.access(d, os.W_OK)


def _make_state_dir() -> None:
    d = _state_dir()
    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    cmds = [[*_sudo(), "mkdir", "-p", d]]
    if user:
        cmds.append([*_sudo(), "chown", "-R", user, d])
    cmds.append([*_sudo(), "chmod", "750", d])
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed: {(r.stderr or '')[-200:]}")


def _java_ok(min_major: int = 21) -> bool:
    if not _has("java"):
        return False
    try:
        out = _run(["java", "-version"], timeout=30).stderr or ""
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
    required: bool = True


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""


def _essentials_present() -> bool:
    return all(_has(b) for b in ("curl", "ps"))


PREREQS: List[Prerequisite] = [
    Prerequisite("os-essentials", 10, _essentials_present,
                 lambda: _install("essentials")),
    Prerequisite("jre-21", 20, _java_ok,
                 lambda: _install("jre")),
    Prerequisite("python3", 30, lambda: _has("python3"),
                 lambda: _install("python3")),
    Prerequisite("container-runtime", 40, lambda: _has("docker"),
                 lambda: _install("docker"), required=False),
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
            except Exception as e:
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
