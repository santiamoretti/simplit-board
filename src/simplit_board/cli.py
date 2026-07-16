from __future__ import annotations

import os
import sys
import time

import click

from . import identity as idmod
from . import naming, registrar, verify
from .auth import TokenProvider
from .bootstrap import Bootstrapper
from .config import load_config
from .presence import PresenceClient
from .state import DeviceState, load_state, save_state
from .supervisor import Supervisor


def _ensure_identity(cfg, st: DeviceState) -> DeviceState:
    """Generate + persist a friendly name and Ed25519 identity on first run; reuse thereafter."""
    ident = idmod.load_or_create(cfg.key_path)
    if not st.name:
        st.name = naming.generate_name()
        st.org = cfg.org
    st.public_key = ident.public_b64u()
    save_state(cfg.state_path, st)
    return st


@click.group()
def main() -> None:
    """SimplitSecurity board agent."""


@main.command()
def bootstrap() -> None:
    """Install the OS-level prerequisites the Java service needs to run (JRE, python3, container runtime).

    Run once when provisioning a board. Idempotent — anything already present is left alone.
    """
    results = Bootstrapper(log=click.echo).run()
    ok = Bootstrapper.ok(results)
    click.echo("")
    for r in results:
        click.echo(f"  {r.name:20s} {r.status}")
    if not ok:
        click.echo("\nsome REQUIRED prerequisites are missing — the Java service can't run until they're installed.", err=True)
        sys.exit(1)
    click.echo("\nbootstrap complete — the box is ready to run the Java service.")


def _choose_placement(cfg, op_token: str, preselected: str | None) -> str | None:
    """Ask the operator WHERE this board should live — the org root or one of the org's subdivisions. Returns the
    chosen parent resource id, or None for the org root (the default). ``preselected`` (a subdivision id or name)
    skips the prompt for non-interactive enrolls."""
    try:
        targets = registrar.list_targets(cfg.targets_url, op_token)
    except registrar.RegistrationError as e:
        click.echo(f"  (couldn't list subdivisions: {e}) — placing under the organization root", err=True)
        return None
    if preselected:
        for t in targets:
            if preselected in (t.get("id"), t.get("name")):
                click.echo(f"placement   : {t.get('name')}  [{t.get('id')}]")
                return t.get("id")
        click.echo(f"  subdivision '{preselected}' not found — placing under the organization root", err=True)
        return None
    if not targets:
        click.echo("placement   : organization root (no subdivisions exist yet)")
        return None
    click.echo("\nwhere should this board go?")
    click.echo("  0) organization root (top level)")
    for i, t in enumerate(targets, 1):
        click.echo(f"  {i}) {t.get('name')}   [{t.get('id')}]")
    idx = click.prompt("  pick a location", type=click.IntRange(0, len(targets)), default=0)
    if idx == 0:
        return None
    chosen = targets[idx - 1]
    click.echo(f"placement   : {chosen.get('name')}  [{chosen.get('id')}]")
    return chosen.get("id")


def _complete_login(cfg, res: dict, mfa_code: str | None) -> str:
    """Turn an auth LoginResult into an operator token, walking the 2FA challenge if the account has one.
    Handles both an already-enrolled account (enter the 6-digit code) and a first-time forced enrollment
    (add the shown secret to an authenticator app, then enter the code it generates)."""
    token = res.get("token")
    if token:
        return token

    if res.get("mfaRequired") and res.get("mfaToken"):
        click.echo("\nthis account has two-factor authentication (2FA) enabled.")
        code = (mfa_code or click.prompt("  6-digit code from your authenticator app")).strip()
        return registrar.mfa_verify(cfg.mfa_verify_url, res["mfaToken"], code)

    if res.get("mfaEnrollmentRequired") and res.get("mfaEnrollmentToken"):
        setup = registrar.mfa_setup(cfg.mfa_setup_url, res["mfaEnrollmentToken"])
        click.echo("\nthis account must set up two-factor authentication (2FA) first.")
        click.echo("add this secret to your authenticator app (Google Authenticator / Authy / 1Password / …):")
        click.echo(f"\n    {setup.get('secret')}\n")
        code = (mfa_code or click.prompt("  then enter the 6-digit code it shows")).strip()
        out = registrar.mfa_enroll(cfg.mfa_enroll_url, res["mfaEnrollmentToken"], code)
        codes = out.get("backupCodes") or []
        if codes:
            click.echo("\nSAVE these one-time backup codes somewhere safe (they are shown only once):")
            for c in codes:
                click.echo(f"    {c}")
            click.echo("")
        return out["token"]
    raise registrar.RegistrationError("sign-in returned no token and no 2FA challenge")


@main.command()
@click.option("--email", default=None, help="operator email (prompted if omitted)")
@click.option("--password", default=None, help="operator password (prompted if omitted; use the prompt, don't put it in shell history)")
@click.option("--subdivision", default=None, help="place the board under this subdivision (id or name); prompts if omitted")
@click.option("--mfa-code", default=None, help="2FA code, if your account has it (prompted if omitted)")
def register(email: str | None, password: str | None, subdivision: str | None, mfa_code: str | None) -> None:
    """Enrol this device by signing in as an operator.

    You are prompted for your SimplitSecurity email + password. That sign-in is the authorization: the
    enrollment service checks you hold the ``enrollDevice`` permission, then mints this device's credential and
    creates its resource under your org. No side scripts, no hand-provisioned secrets — sign in and you're done.
    """
    cfg = load_config()
    st = _ensure_identity(cfg, load_state(cfg.state_path))
    click.echo(f"device name : {st.name}")
    click.echo(f"org         : {st.org}")
    click.echo(f"public key  : {st.public_key}")

    secret = os.environ.get("SIMPLIT_DEVICE_SECRET") or st.client_secret
    if not secret:

        click.echo("\nsign in to enrol this device (your account must hold the enrollDevice permission):")
        email = email or click.prompt("  operator email")
        password = password or click.prompt("  operator password", hide_input=True)
        try:
            login_res = registrar.login(cfg.login_url, email.strip(), password)
            op_token = _complete_login(cfg, login_res, mfa_code)
            parent = _choose_placement(cfg, op_token, subdivision)
            result = registrar.enroll(cfg.enroll_url, op_token, st.name, st.public_key or "",
                                      parent_resource_id=parent)
        except registrar.RegistrationError as e:
            click.echo(f"\nenrollment failed: {e}", err=True)
            sys.exit(2)
        secret = result["clientSecret"]
        st.org = result.get("org") or st.org
        st.client_id = result.get("clientId") or st.name
        where = "the organization root" if not parent else f"subdivision {parent}"
        click.echo(f"enrolled    : credential minted + resource created under {where}  ✓")


        anchor = (result.get("signingPubkey") or "").strip()
        if anchor:
            st.delivery_pubkey = anchor
            click.echo("trust anchor: build signing key received from enrollment + stored  ✓")
        else:
            click.echo("trust anchor: enrollment returned no signing key — set SIMPLIT_DELIVERY_PUBKEY before "
                       "`up`, or ask an operator to configure enrollment.signing-pubkey.", err=True)


    tokens = TokenProvider(cfg.token_url, st.client_id or st.name, secret)
    try:
        tokens.current()
    except Exception as e:
        click.echo(f"\nwarning: device credential did not mint a token yet: {e}", err=True)
    st.registered = True
    st.client_id = st.client_id or st.name
    st.client_secret = secret
    save_state(cfg.state_path, st)
    click.echo(f"\nregistered  : {st.name}  ✓  (visible to operators who can read this org — run `simplit-board up`)")


@main.command()
def up() -> None:
    """Bring the device online — with NO software on it. The board holds the presence session and waits: the
    first signed push from the operator's console is how it gets its software.

    A device has one presence session. On boot the AGENT holds it and does exactly one job — receive control's
    signed push, verify the signature, and install + start the board service. It then yields the session to
    that board service, which reconnects as the same device and owns the channel from then on (report queries +
    future updates). So a fresh board is genuinely empty until an operator pushes to it — no pre-loaded jar.
    """
    cfg = load_config()
    st = load_state(cfg.state_path)
    if not st.registered or not st.name:
        click.echo("not registered — run `simplit-board register` first.", err=True)
        sys.exit(2)
    secret = os.environ.get("SIMPLIT_DEVICE_SECRET") or st.client_secret
    if not secret:
        click.echo("no device credential — run `simplit-board register` first.", err=True)
        sys.exit(2)


    delivery_pub_b64 = st.delivery_pubkey or cfg.trusted_delivery_pubkey or ""
    delivery_key = verify.load_control_key(delivery_pub_b64) if delivery_pub_b64 else None
    control_key = verify.load_control_key(cfg.trusted_control_pubkey) if cfg.trusted_control_pubkey else None
    if delivery_key is None and control_key is None:
        click.echo("no trusted signing key (SIMPLIT_DELIVERY_PUBKEY / SIMPLIT_CONTROL_PUBKEY) — refusing to "
                   "accept unsigned deploys.", err=True)
        sys.exit(2)

    supervisor = Supervisor(cfg.jar_dir, device_id=st.name, device_secret=secret,
                            token_url=cfg.token_url, presence_url=cfg.presence_ws_url,
                            register_url=cfg.register_url, delivery_pubkey=delivery_pub_b64)

    def handle_artifact(manifest: dict, data: bytes) -> dict:

        click.echo(f"[deploy] verified {len(data)} bytes ({manifest.get('service')} v={manifest.get('version')}) "
                   "— installing the board service from the received bytes …")
        detail = supervisor.deploy_bytes(data, version=str(manifest.get("version") or "pushed"))
        return {"boardId": st.name, "status": "deployed", "detail": detail}

    def handle_push(frame: dict) -> dict:

        job = verify.verify_job(control_key, frame.get("payload", ""), frame.get("signature", ""))
        ref = job.get("imageRef") or job.get("image") or job.get("componentId") or "board service"
        click.echo(f"[push] control signature verified — installing {ref} …")
        detail = supervisor.deploy(job)
        return {"boardId": st.name, "status": "deployed", "detail": detail}

    tokens = TokenProvider(cfg.token_url, st.client_id or st.name, secret)
    presence = PresenceClient(cfg.presence_ws, tokens, st.name,
                              handler=handle_push if control_key else None, log=click.echo,
                              yield_after_deploy=True,
                              artifact_handler=handle_artifact, delivery_key=delivery_key)
    click.echo(f"board '{st.name}' is online — no software installed yet.")
    click.echo("waiting for the operator to push the board service from the console (Updates ▸ push to this "
               "device)…  Ctrl-C to stop")
    try:
        presence.run_forever()
    except KeyboardInterrupt:
        presence.stop()
        click.echo("\nstopped before any push arrived.", err=True)
        return

    if not supervisor.status().get("running"):
        click.echo("push did not result in a running board service — see the log above.", err=True)
        return


    ephemeral = os.environ.get("SIMPLIT_AGENT_EPHEMERAL", "true").strip().lower() in ("1", "true", "yes")
    if ephemeral:
        click.echo(
            f"\nboard '{st.name}' installed + running — it now owns the presence session and receives its own "
            "updates directly (re-push goes straight to the board → verify → self-restart). The agent's job is "
            "done; exiting. The board is self-managed from here.")
        return
    click.echo("board service installed + running (agent supervising). Ctrl-C to stop.")
    try:
        while supervisor.status().get("running"):
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    click.echo("board service exited.", err=True)


@main.command()
def status() -> None:
    """Show local identity + deployed service state."""
    cfg = load_config()
    st = load_state(cfg.state_path)
    click.echo(f"name        : {st.name}")
    click.echo(f"org         : {st.org}")
    click.echo(f"registered  : {st.registered}")
    click.echo(f"version     : {st.current_version}")
    click.echo(f"state dir   : {cfg.state_dir}")
    click.echo(f"presence    : {cfg.presence_ws}")


if __name__ == "__main__":
    main()
