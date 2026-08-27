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
simplit-board register            # 2. sign in as an operator; names + mints this device
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

It also asks what to **call** this board — a name people will recognise in the console, like
`Redacción, piso 3`. Pass it with `--name` to skip the prompt, or leave it blank: the name is cloud-side
metadata, so it can be set or changed later from the platform without ever touching the appliance again. The
generated id stays the board's identity regardless.

You are asked where to place it, too. That placement decides which organization the board **belongs** to —
it is read from the tree, not from who ran the command — so a partner enrolling a board into a customer's
subdivision produces a board that belongs to the customer.

**3. `install-service`** registers the agent with systemd so the board comes back by itself.

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

An appliance has to come back on its own. `bootstrap` therefore registers the Java service with systemd as
`simplit-board-service` and enables it, so the board runs its software again the moment the machine boots — no
agent, no operator, no re-push. Check it with `systemctl status simplit-board-service`.

Writing that unit needs root, which is exactly why it happens during `bootstrap`: that is the one step an
operator runs by hand and can authorise. Everything after it is unprivileged. The unit does not carry the
board's settings — it reads them from `board.env` next to the jar, which the service rewrites every time it
starts. So the settings change with every deploy while the unit never has to, and no software update ever
needs root again.

The agent is a kickstarter, not a supervisor: once that service is up, `simplit-board up` sees it and stands
down rather than contending for the device's single presence session. Re-pushes go straight to the board, which
verifies and restarts itself.

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
should print `enabled` twice. If the second is missing, `bootstrap` never got to install it — re-run
`sudo simplit-board bootstrap`, which is idempotent and will only do the step that is absent.

If the unit is enabled and the board still comes back empty, check that `board.env` exists next to the jar
(`/var/lib/simplit/service/board.env`) and is not empty. The service writes it on every start, so an absent
file means the service has not run since the agent was upgraded — push the software once and it appears.

**Two boards appear to be running at once.** `pgrep -af app.jar` should show exactly one. A stray older
process holds a second presence session, and the device's replies then go to whichever one asked; kill the
older PID. Recent board builds refuse this state at birth: a second instance exits immediately with a message
naming the PID that already holds the store's lock file (`board.db.lock`), so on a current build "two boards"
can only mean the OLD build is the survivor — which is itself the diagnosis.

**You launched the board by hand, and now `systemctl restart` "does nothing".** A board started from an SSH
session belongs to that session, not to systemd — so `systemctl restart simplit-board-service` stops and
starts *systemd's* instance while the hand-launched one keeps running, keeps the presence session, and
ignores every restart forever. This exact state ran undiagnosed on a lab appliance for two days: the console
showed the board online the whole time. If you must run it by hand for debugging, stop the unit first
(`sudo systemctl stop simplit-board-service`) and start the unit again when you are done. Recent builds print
a warning at the top of the log when they detect a hand launch beside an installed unit, and refuse to run
at all if another instance already holds the store.

**The board shows ONLINE in the console but ignores every command and push.** The "online" light is the HTTP
heartbeat; commands ride a separate WebSocket (the presence session), and the two can fail independently. On
older builds, a connection the network killed abruptly (a reset, not a clean close) silenced the command
channel *permanently* while the heartbeat kept the console green — the only trace being a single
`presence socket error` line in `/var/lib/simplit/service/service.log` with no reconnection after it. Current
builds reconnect on every way the socket can die, detect the half-open case with a pong deadline, and log
`command channel is DOWN` every minute until it is back — so on a current build this state announces itself.
On an older one: `sudo systemctl restart simplit-board-service` restores the channel; then update the board.

**A wiped board is a NEW device.** The identity lives in `/var/lib/simplit`; delete that directory and the
next `register` mints a fresh name and a fresh credential. The old device does not disappear on its own — it
lingers in the console as an offline board until an operator un-enrols it. Wipe, re-register, then remove the
old entry, in that order, and expect scans/reports/settings scoped to the old name to stay with the old name.

**A `systemctl status` or `journalctl` left open over SSH.** Both page their output; over a dropped SSH
session the pager never exits, and the orphaned processes accumulate (three of them sat wedged on a lab
appliance for two days). In scripts and remote shells, pass `--no-pager`.

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
