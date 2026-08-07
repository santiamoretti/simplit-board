# simplit-board — the appliance agent

A small terminal tool that runs on a SimplitSecurity board. It gives the device a friendly, generated identity
(`ancient-binder-4821` style), registers it in the cloud, and then receives control's **signed** Java-service
pushes over the presence relay — verifying each one against control's public key before deploying and
supervising the Java process. The Java service is what runs the actual security tooling; the agent only owns
the device identity and the update channel.

## Install

```
pipx install git+https://github.com/santiamoretti/simplit-board.git
```

Use **pipx**, not `pip install --user`. An older setuptools builds this project into a wheel named
`UNKNOWN-0.0.0` — it installs, and then nothing works because the console script was never created. pipx
brings its own current build tooling, so the failure cannot happen.

From a local checkout it is `pipx install .` for the same reason.

## Use

```
simplit-board register     # mint identity (if new) + register this device in the cloud
simplit-board up           # connect to the presence relay; receive → verify → deploy Java pushes
simplit-board status       # show local identity + deployed service version
```

Identity is generated once and persisted under `SIMPLIT_STATE_DIR` (default `/var/lib/simplit`), written
atomically so a power cut never mints a new device on reboot.

## Surviving a power cut

An appliance has to come back on its own. The first deploy therefore registers the Java service with systemd as
`simplit-board-service` and enables it, so the board is running its software again the moment the machine boots
— no agent, no operator, no re-push. Check it with `systemctl status simplit-board-service`.

The agent is a kickstarter, not a supervisor: once that service is up, `simplit-board up` sees it and stands
down rather than contending for the device's single presence session. Re-pushes go straight to the board, which
verifies and restarts itself.

Registering the unit needs root (or passwordless `sudo`). Without it the deploy still succeeds and the service
still runs — but it prints a warning, and it will **not** come back after a power cut.

## Enrollment

A device does **not** self-enroll anonymously. `register` generates the device's name + Ed25519 key and, if it
has no credential yet, prints an enrollment request and exits. An operator provisions a service-account
credential (`clientId` = the device name) out of band; set `SIMPLIT_DEVICE_SECRET` and re-run `register`.

## Configuration (env)

| var | default | meaning |
|-----|---------|---------|
| `SIMPLIT_DOMAIN` | live cloud | Container Apps domain; derives the auth/gateway/presence URLs |
| `SIMPLIT_ORG` | `simplit` | org the device belongs to |
| `SIMPLIT_CONTROL_PUBKEY` | — | base64 Ed25519 key that verifies control's pushes (**required for `up`**) |
| `SIMPLIT_DEVICE_SECRET` | — | the device's service-account secret |
| `SIMPLIT_STATE_DIR` | `/var/lib/simplit` | where identity + state persist |
| `SIMPLIT_BOARD_JAR` | — | baked Java service to run when a push carries no artifact URL |
