from __future__ import annotations
import time
import requests
from .auth import TokenProvider

class RegistrationError(RuntimeError):
    pass

def login(login_url: str, email: str, password: str, timeout: float=45.0, attempts: int=5) -> str:
    """Authenticate the operator; return their access token. That token IS the enrollment authorization —
    the enrollment service gates on the operator holding the enrollDevice permission.

    Cold-start resilient: the auth service scales to zero, so the first sign-in after idle can time out or
    reset. We retry those (and 5xx) with backoff. A 4xx (bad credentials) is a real rejection — no retry.
    """
    last = None
    delay = 3.0
    for i in range(attempts):
        try:
            resp = requests.post(login_url, json={'email': email, 'password': password}, headers={'Content-Type': 'application/json'}, timeout=timeout)
            if resp.status_code // 100 == 2:
                body = resp.json()
                token = body.get('token')
                if not token:
                    if body.get('passwordChangeRequired') or body.get('mustChangePassword'):
                        raise RegistrationError('this account must set a new password first — sign in on the web app once, then retry.')
                    raise RegistrationError('sign-in returned no token')
                return token
            if resp.status_code == 429:
                last = RegistrationError('sign-in rate-limited (HTTP 429)')
            elif resp.status_code // 100 == 5:
                last = RegistrationError(f'sign-in -> HTTP {resp.status_code}: {resp.text[:160]}')
            else:
                raise RegistrationError(f'sign-in failed: HTTP {resp.status_code} {resp.text[:160]}')
        except (requests.Timeout, requests.ConnectionError) as e:
            last = e
        if i < attempts - 1:
            time.sleep(delay)
            delay = min(delay * 1.7, 20.0)
    raise RegistrationError(f'sign-in failed after {attempts} attempts (auth may be cold): {last}')

def list_targets(targets_url: str, operator_token: str, timeout: float=45.0, attempts: int=5) -> list:
    """The subdivisions this board may be placed under, as [{id, name, parentId, type}]. The enrollment service
    pulls them from tenancy on its own authority (it holds readStructure over tenancy) and gates the call on the
    operator's enrollDevice. Cold-start resilient (5xx/timeout retried); a 401/403 is surfaced so the caller can
    fall back to the org root rather than spin.
    """
    last = None
    delay = 3.0
    for i in range(attempts):
        try:
            resp = requests.get(targets_url, headers={'Authorization': f'Bearer {operator_token}', 'Accept': 'application/json'}, timeout=timeout)
            if resp.status_code // 100 == 2:
                body = resp.json()
                return body if isinstance(body, list) else body.get('data', [])
            if resp.status_code in (401, 403):
                raise RegistrationError(f'not authorized to list placement options (HTTP {resp.status_code})')
            if resp.status_code // 100 == 5:
                last = RegistrationError(f'targets -> HTTP {resp.status_code}: {resp.text[:160]}')
            else:
                raise RegistrationError(f'targets -> HTTP {resp.status_code}: {resp.text[:200]}')
        except (requests.Timeout, requests.ConnectionError) as e:
            last = e
        if i < attempts - 1:
            time.sleep(delay)
            delay = min(delay * 1.7, 20.0)
    raise RegistrationError(f'listing placement options failed after {attempts} attempts: {last}')

def enroll(enroll_url: str, operator_token: str, device_name: str, device_pub_b64: str='', parent_resource_id: str | None=None, timeout: float=60.0, attempts: int=6) -> dict:
    """Enrol the device via the engine-gated enrollment service. Returns {clientId, clientSecret, org,
    signingPubkey} — signingPubkey being the board's code-signing trust anchor, delivered in-band.

    ``parent_resource_id`` is the chosen placement (a subdivision id); omitted/None means the org root (the
    engine authorizes CREATE on that exact parent at intent time). The operator's token authorizes it (the
    service runs the enrollDevice two-gate). Cold-start resilient — the service scales to zero, so the first
    call after idle can take tens of seconds. A 401/403 is a real authorization failure (the operator lacks
    enrollDevice, or CREATE on the chosen parent), not a cold start, so we don't spin on it.
    """
    last = None
    delay = 3.0
    body = {'deviceName': device_name, 'devicePubB64': device_pub_b64}
    if parent_resource_id:
        body['parentResourceId'] = parent_resource_id
    for i in range(attempts):
        try:
            resp = requests.post(enroll_url, json=body, headers={'Authorization': f'Bearer {operator_token}', 'Content-Type': 'application/json'}, timeout=timeout)
            if resp.status_code // 100 == 2:
                body = resp.json()
                if not body.get('clientSecret'):
                    raise RegistrationError('enrollment succeeded but returned no credential')
                return body
            if resp.status_code in (401, 403):
                raise RegistrationError(f'not authorized to enrol devices (HTTP {resp.status_code}) — your account needs the enrollDevice permission.')
            if resp.status_code // 100 == 5:
                last = RegistrationError(f'enroll -> HTTP {resp.status_code}: {resp.text[:160]}')
            else:
                raise RegistrationError(f'enroll -> HTTP {resp.status_code}: {resp.text[:200]}')
        except (requests.Timeout, requests.ConnectionError) as e:
            last = e
        if i < attempts - 1:
            time.sleep(delay)
            delay = min(delay * 1.7, 20.0)
    raise RegistrationError(f'enrollment failed after {attempts} attempts: {last}')

def register(register_url: str, tokens: TokenProvider, timeout: float=60.0, attempts: int=8) -> str:
    """POST the empty-body registration; return the deviceId the gateway confirms.

    Cold-start resilient: the gateway scales to zero, so the first call after idle can take tens of seconds or
    reset the connection. We retry with backoff (mirroring the board's scheduled re-registration) so a fresh
    device reliably registers even against a cold cloud.
    """
    last = None
    delay = 3.0
    for i in range(attempts):
        try:
            resp = requests.post(register_url, json={}, headers={'Authorization': f'Bearer {tokens.current()}', 'Content-Type': 'application/json'}, timeout=timeout)
            if resp.status_code // 100 == 2:
                try:
                    return resp.json().get('data') or ''
                except Exception:
                    return ''
            if resp.status_code // 100 == 5:
                last = RegistrationError(f'register -> HTTP {resp.status_code}: {resp.text[:160]}')
            else:
                raise RegistrationError(f'register {register_url} -> HTTP {resp.status_code}: {resp.text[:200]}')
        except (requests.Timeout, requests.ConnectionError) as e:
            last = e
        if i < attempts - 1:
            time.sleep(delay)
            delay = min(delay * 1.7, 20.0)
    raise RegistrationError(f'registration failed after {attempts} attempts: {last}')

def confirm_visible(devices_url: str, bearer: str, device_id: str, timeout: float=30.0) -> bool:
    """Optional: check the device now appears in the operator's devices list (with a user token)."""
    resp = requests.get(devices_url, headers={'Authorization': f'Bearer {bearer}'}, timeout=timeout)
    if resp.status_code // 100 != 2:
        return False
    data = resp.json()
    rows = data.get('data') if isinstance(data, dict) else data
    return any((r.get('id') == device_id for r in rows or []))
