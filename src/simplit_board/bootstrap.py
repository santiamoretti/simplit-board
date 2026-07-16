from __future__ import annotations
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, List

def _run(cmd: List[str], timeout: float=600.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

def _has(binary: str) -> bool:
    return shutil.which(binary) is not None

def _apt_available() -> bool:
    return _has('apt-get')

def _sudo() -> List[str]:
    if os.geteuid() == 0:
        return []
    return ['sudo'] if _has('sudo') else []

def _apt_install(*packages: str) -> None:
    if not _apt_available():
        raise RuntimeError('no apt-get on this system — install %s manually' % ', '.join(packages))
    env = dict(os.environ, DEBIAN_FRONTEND='noninteractive')
    subprocess.run([*_sudo(), 'apt-get', 'update', '-qq'], check=False, env=env, timeout=300)
    r = subprocess.run([*_sudo(), 'apt-get', 'install', '-y', '--no-install-recommends', *packages], env=env, timeout=900, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'apt install {' '.join(packages)} failed: {r.stderr[-300:]}')

def _java_ok(min_major: int=21) -> bool:
    if not _has('java'):
        return False
    try:
        out = _run(['java', '-version'], timeout=30).stderr or ''
        for tok in out.replace('"', ' ').split():
            if tok[:1].isdigit():
                return int(tok.split('.')[0]) >= min_major
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
    detail: str = ''

def _essentials_present() -> bool:
    return all((_has(b) for b in ('curl', 'ps')))
PREREQS: List[Prerequisite] = [Prerequisite('os-essentials', 10, _essentials_present, lambda: _apt_install('ca-certificates', 'curl', 'procps', 'util-linux')), Prerequisite('jre-21', 20, _java_ok, lambda: _apt_install('openjdk-21-jre-headless')), Prerequisite('python3', 30, lambda: _has('python3'), lambda: _apt_install('python3', 'python3-venv')), Prerequisite('container-runtime', 40, lambda: _has('docker'), lambda: _apt_install('docker.io'), required=False)]

class Bootstrapper:

    def __init__(self, prereqs: List[Prerequisite] | None=None, log=print):
        self.prereqs = sorted(prereqs or PREREQS, key=lambda p: p.order)
        self.log = log

    def run(self) -> List[Result]:
        results: List[Result] = []
        for p in self.prereqs:
            try:
                if p.check():
                    self.log(f'[bootstrap] {p.name}: already present')
                    results.append(Result(p.name, 'already'))
                    continue
                self.log(f'[bootstrap] {p.name}: installing…')
                p.install()
                if p.check():
                    self.log(f'[bootstrap] {p.name}: installed')
                    results.append(Result(p.name, 'installed'))
                else:
                    raise RuntimeError('still not satisfied after install')
            except Exception as e:
                status = 'failed' if p.required else 'skipped'
                self.log(f'[bootstrap] {p.name}: {status} — {e}')
                results.append(Result(p.name, status, str(e)[:200]))
        return results

    @staticmethod
    def ok(results: List[Result]) -> bool:
        """True if every REQUIRED prerequisite ended up satisfied."""
        return all((r.status in ('already', 'installed') for r in results if r.status != 'skipped'))

def java_ready() -> bool:
    """Cheap preflight the deploy path calls before launching the jar."""
    return _java_ok()
