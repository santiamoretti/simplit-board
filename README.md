# simplit-board — the appliance agent

A small terminal tool that runs on a SimplitSecurity board. It gives the device a friendly, generated identity
(`ancient-binder-4821` style), enrols it in the cloud, and then receives control's **signed** Java-service
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

If the shell then says `simplit-board: command not found`, pipx put it somewhere not on your `PATH`. Fix it
once with `pipx ensurepath` and open a new shell, or use `~/.local/bin/simplit-board` directly.

## Bringing a board up

Four steps, in order. Each one is idempotent — re-running it on a board that is already there does nothing.

```
sudo simplit-board bootstrap      # 1. OS prerequisites (needs root)
simplit-board register            # 2. sign in as an operator; mints this device
sudo simplit-board install-service # 3. start on boot
simplit-board up                  # 4. online, waiting for the software push
```

Then push the Java service to it from the console (Updates ▸ push to this device). The board installs it,
starts it, and from there provisions its own tooling — scanner, network monitor, report interpreter.

**1. `bootstrap`** installs what the Java service needs and cannot install for itself: a JRE, python3, the
container runtime **with its compose plugin**, and the directories the board writes into. It also applies the
settings that keep an appliance alive rather than merely running — bounded container logs, containers that
survive a daemon restart, a container address pool that does not collide with the network the board was
installed to watch, and OOM protection for the container daemon and sshd. Existing configuration is merged,
never replaced.

**2. `register`** asks for your SimplitSecurity email and password. That sign-in *is* the authorization: the
enrollment service checks you hold the `enrollDevice` permission, then mints this device's credential and
creates its resource under your organization. Nothing is provisioned by hand, and the trust anchors for
future updates are pinned from that same authenticated response.

**3. `install-service`** registers the agent with systemd so the board comes back by itself. The first push
additionally registers the Java service the same way — see below.

**4. `up`** holds the device's presence session and waits. A fresh board is genuinely empty until an operator
pushes to it; there is no pre-loaded software.

## Use

```
simplit-board status       # local identity + deployed service state
simplit-board up           # connect to the relay; receive → verify → deploy pushes
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

Registering that unit needs root, and the deploy reaches for `sudo -n` (non-interactive). On a box where the
board's user has no passwordless sudo the deploy still succeeds and the service still runs — but it prints a
warning, and the board will **not** come back after a power cut. See the troubleshooting entry below.

## Troubleshooting

Real failures, in the order they tend to happen.

**`simplit-board: command not found` after installing.** pipx installed into `~/.local/bin`, which is not on
your `PATH`. `pipx ensurepath`, then open a new shell. Under `sudo` the same applies to root's `PATH` — either
call the binary by its full path or pass the environment through:
`sudo env "PATH=$PATH" simplit-board bootstrap`.

**`docker: unknown command: docker compose`.** The engine is installed but its compose plugin is not — they
are separate packages, and the distribution's `docker.io` ships none. `bootstrap` installs the engine and the
plugin together from Docker's own repository, verifying the signing key's fingerprint first. Note that the
hyphenated `docker-compose` (v1) binary does **not** satisfy this: the tooling invokes `docker compose`, the
subcommand.

**`permission denied` on `/var/run/docker.sock`.** The board runs unprivileged and needs to be in the `docker`
group. `bootstrap` adds it, but group membership only applies to *new* logins — log out and back in, or reboot,
before expecting it to take effect.

**`AccessDeniedException: /opt/simplit/...`, or "could not lay down the monitor".** The board could not create
the directory it installs a tool into. That directory belongs to root on most images and the board is not root.
`bootstrap` creates them owned by the operating user; if you are on an older agent, create them yourself:
`sudo install -d -o "$USER" -g "$USER" /opt/simplit/openvas /opt/simplit/suricata /opt/simplit/pysandbox`.

**The monitor container restarts forever, its log saying `No such device`.** It is capturing on an interface
this machine does not have. The agent now picks the interface carrying the default route, but you can always
say which one explicitly in the board's Live Monitor settings — an interface you name is honoured even while
it is down.

**"openvasd is not answering" right after install.** Expected on a first boot: the scanner copies several
gigabytes and parses its feed before it will answer anything. The board waits — up to fifteen minutes by
default — and says so while it waits. Do not restart it into that window.

**The scanner is up but nothing can reach it on port 3000.** Something else on the box already had that port,
so the board moved the scanner to a free one rather than failing. Ask docker where it went:
`docker port simplit-openvasd-openvasd-1`.

**The board does not come back after a reboot.** `systemctl is-enabled simplit-board simplit-board-service`
should print `enabled` twice. If the second is missing, the deploy could not write its unit for lack of
passwordless sudo — grant it, or re-run the deploy from a session that has root.

**Two boards appear to be running at once.** `pgrep -af app.jar` should show exactly one. A stray older
process holds a second presence session, and the device's replies then go to whichever one asked; kill the
older PID.

## Configuration (env)

| var | default | meaning |
|-----|---------|---------|
| `SIMPLIT_DOMAIN` | live cloud | Container Apps domain; derives the auth/gateway/presence URLs |
| `SIMPLIT_ORG` | `simplit` | org the device belongs to |
| `SIMPLIT_CONTROL_PUBKEY` | pinned at enrollment | base64 Ed25519 key that verifies control's pushes |
| `SIMPLIT_DEVICE_SECRET` | minted at enrollment | the device's service-account secret |
| `SIMPLIT_STATE_DIR` | `/var/lib/simplit` | where identity + state persist |
| `SIMPLIT_BOARD_JAR` | — | baked Java service to run when a push carries no artifact URL |

The two keys marked *at enrollment* are pinned in-band from the authenticated `register` response and stored;
setting them by hand is a fallback, not the normal path.
