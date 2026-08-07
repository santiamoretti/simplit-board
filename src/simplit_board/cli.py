"""``simplit-board`` — the appliance's terminal tool.

    simplit-board register   # mint a friendly identity + register this device in the cloud
    simplit-board up         # connect to the presence relay and receive/verify/deploy Java pushes
    simplit-board status     # show the local identity + service state

Identity is generated once and persisted; a reboot re-uses it (never mints a new device).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
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
    # already-enrolled 2FA — ask for the current 6-digit code and redeem the challenge
    if res.get("mfaRequired") and res.get("mfaToken"):
        click.echo("\nthis account has two-factor authentication (2FA) enabled.")
        code = (mfa_code or click.prompt("  6-digit code from your authenticator app")).strip()
        return registrar.mfa_verify(cfg.mfa_verify_url, res["mfaToken"], code)
    # first-time forced 2FA enrollment — show the secret to add to an app, then confirm a code
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
        # Enroll: sign in as an operator, and that authenticated session provisions this device.
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

        # The code-signing trust anchor (the BUILD public key) rides INSIDE this authenticated, engine-gated
        # enrollment response — we pin it from a verified enrollment, never a TOFU first-use fetch. Persist it so
        # `up` needs no manual SIMPLIT_DELIVERY_PUBKEY.
        anchor = (result.get("signingPubkey") or "").strip()
        if anchor:
            st.delivery_pubkey = anchor
            click.echo("trust anchor: build signing key received from enrollment + stored  ✓")
        else:
            click.echo("trust anchor: enrollment returned no signing key — set SIMPLIT_DELIVERY_PUBKEY before "
                       "`up`, or ask an operator to configure enrollment.signing-pubkey.", err=True)

        # Control's key rides the same verified channel. A board without it rejects every control job, and the
        # only symptom is a push that never lands — so say so here, at the one moment an operator is watching.
        control_anchor = (result.get("controlPubkey") or "").strip()
        if control_anchor:
            st.control_pubkey = control_anchor
            click.echo("control key : received from enrollment + stored  ✓")
        else:
            click.echo("control key : enrollment returned none — this board will REJECT control jobs (updates, "
                       "remote commands). Ask an operator to configure enrollment.control-pubkey.", err=True)

    # Confirm the credential works by minting a device token (also warms the device for `up`).
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
    # Trust anchors. The delivery service streams artifact BYTES down here, carrying the CI BUILD signature we
    # verify against the build public key; control may also send legacy signed reference jobs (verified against
    # the control key). At least one is required — the board never installs anything unsigned.
    # The build signing key pinned IN-BAND from the authenticated enrollment (st.delivery_pubkey) is AUTHORITATIVE;
    # a SIMPLIT_DELIVERY_PUBKEY env is only a fallback when nothing was pinned (so a stale env can't shadow the
    # correct pinned anchor). This SAME resolved anchor is handed to the Java board below, so the board verifies
    # its own future pushes against it too — the agent only owns the FIRST push; the Java board owns the rest.
    delivery_pub_b64 = st.delivery_pubkey or cfg.trusted_delivery_pubkey or ""
    delivery_key = verify.load_control_key(delivery_pub_b64) if delivery_pub_b64 else None
    # Same precedence for control's key: what enrollment pinned wins over an env var, so a stale shell can't
    # shadow the verified anchor. The resolved value is handed to the Java board, which does its own verifying.
    control_pub_b64 = st.control_pubkey or cfg.trusted_control_pubkey or ""
    control_key = verify.load_control_key(control_pub_b64) if control_pub_b64 else None
    if delivery_key is None and control_key is None:
        click.echo("no trusted signing key (SIMPLIT_DELIVERY_PUBKEY / SIMPLIT_CONTROL_PUBKEY) — refusing to "
                   "accept unsigned deploys.", err=True)
        sys.exit(2)

    supervisor = Supervisor(cfg.jar_dir, device_id=st.name, device_secret=secret,
                            token_url=cfg.token_url, presence_url=cfg.presence_ws_url,
                            register_url=cfg.register_url, delivery_pubkey=delivery_pub_b64,
                            control_pubkey=control_pub_b64, flows_url=cfg.flows_url,
                            gateway_url=cfg.gateway_url, integrations_url=cfg.integrations_url)

    def handle_artifact(manifest: dict, data: bytes) -> dict:
        # Bytes arrived + were already signature/hash-verified by the presence client. Run them, fetching nothing.
        click.echo(f"[deploy] verified {len(data)} bytes ({manifest.get('service')} v={manifest.get('version')}) "
                   "— installing the board service from the received bytes …")
        detail = supervisor.deploy_bytes(data, version=str(manifest.get("version") or "pushed"))
        return {"boardId": st.name, "status": "deployed", "detail": detail}

    def handle_push(frame: dict) -> dict:
        # Legacy control-signed reference job (kept for compatibility).
        job = verify.verify_job(control_key, frame.get("payload", ""), frame.get("signature", ""))
        ref = job.get("imageRef") or job.get("image") or job.get("componentId") or "board service"
        click.echo(f"[push] control signature verified — installing {ref} …")
        detail = supervisor.deploy(job)
        return {"boardId": st.name, "status": "deployed", "detail": detail}

    # A board that has ALREADY been provisioned belongs to its board service, not to this agent. After a
    # power cut systemd brings that service back (see Supervisor._install_boot_unit) and it takes the
    # device's single presence session; an agent that also connected would fight it for the channel. So
    # when the service is up, the agent stands down instead of announcing itself as an empty board.
    if supervisor.boot_unit_active():
        click.echo(f"board '{st.name}' already runs its service (systemd) — agent standing down. "
                   "Re-pushes go straight to the board, which verifies and self-restarts.")
        return

    tokens = TokenProvider(cfg.token_url, st.client_id or st.name, secret)
    presence = PresenceClient(cfg.presence_ws, tokens, st.name,
                              handler=handle_push if control_key else None, log=click.echo,
                              yield_after_deploy=True,
                              artifact_handler=handle_artifact, delivery_key=delivery_key)
    click.echo(f"board '{st.name}' is online — no software installed yet.")
    click.echo("waiting for the operator to push the board service from the console (Updates ▸ push to this "
               "device)…  Ctrl-C to stop")
    try:
        presence.run_forever()  # returns once a push is verified + installed and we've yielded the session
    except KeyboardInterrupt:
        presence.stop()
        click.echo("\nstopped before any push arrived.", err=True)
        return

    if not supervisor.status().get("running"):
        click.echo("push did not result in a running board service — see the log above.", err=True)
        return

    # The agent is a one-shot kickstarter. Once the board service is up (it owns presence and will receive its
    # own future updates directly — a re-push goes straight to the board, which verifies + self-restarts), the
    # agent has served its purpose and exits. The board runs under a detached restart loop, so it outlives us.
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


def _sudo() -> list[str]:
    if os.geteuid() == 0:
        return []
    if shutil.which("sudo") is None:
        raise click.ClickException("need root (or sudo) to install a systemd service")
    return ["sudo"]


@main.command("install-service")
@click.option("--user", default=None, help="user to run the service as (default: SUDO_USER / current user)")
def install_service(user: str | None) -> None:
    """Install + enable a systemd service so `simplit-board up` runs on boot and reconnects on its own.

    Detects this install's `simplit-board` executable, the operating user and the state dir, writes
    /etc/systemd/system/simplit-board.service, then daemon-reloads and enables+starts it.
    """
    exe = shutil.which("simplit-board") or os.path.join(os.path.dirname(sys.executable), "simplit-board")
    if not os.path.exists(exe):
        raise click.ClickException(f"could not find the simplit-board executable ({exe}); install the package first")
    run_user = user or os.environ.get("SUDO_USER") or os.environ.get("USER") or os.environ.get("LOGNAME") or "root"
    state_dir = os.environ.get("SIMPLIT_STATE_DIR", "/var/lib/simplit")
    unit = (
        "[Unit]\n"
        "Description=Simplit Board agent\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={run_user}\n"
        f"Environment=SIMPLIT_STATE_DIR={state_dir}\n"
        f"ExecStart={exe} up\n"
        # on-failure, NOT always. The agent is a one-shot kickstarter: once the board service exists it
        # stands down and exits 0, and under Restart=always that clean exit became a restart every 5
        # seconds forever — measured at 588 restarts on a provisioned board, filling the journal and
        # hiding any failure that mattered. A crash still restarts; finishing the job does not. What
        # keeps the appliance running from here is the board service's own Restart=always.
        "Restart=on-failure\n"
        "RestartSec=5\n"
        # The agent's ONE job is to receive the first push, install the board service, and hand the
        # presence session over to it — so it exits, and systemd restarts it. Under the default
        # KillMode=control-group that restart kills every process in the unit's cgroup, INCLUDING the
        # board service the agent just started. Measured on a fresh board: the service booted
        # completely (Tomcat up, registered, presence connected) and was killed 0.4s later, leaving a
        # board that looked deployed and ran nothing. KillMode=process signals only the agent.
        "KillMode=process\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    path = "/etc/systemd/system/simplit-board.service"
    sudo = _sudo()
    with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False) as tf:
        tf.write(unit)
        tmp = tf.name
    try:
        subprocess.run([*sudo, "cp", tmp, path], check=True, timeout=30)
    finally:
        os.unlink(tmp)
    subprocess.run([*sudo, "systemctl", "daemon-reload"], check=True, timeout=30)
    subprocess.run([*sudo, "systemctl", "enable", "--now", "simplit-board"], check=True, timeout=60)
    click.echo(f"installed + started 'simplit-board' service  ✓  (user={run_user}, exec={exe} up)")
    click.echo("check it with:  sudo systemctl status simplit-board")


if __name__ == "__main__":
    main()
