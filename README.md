# simplit-board

A small terminal agent that runs on a SimplitSecurity board (appliance). It gives the device a generated
identity, enrolls it in the cloud under an operator's account, and then holds the presence channel to receive
**signed** Java-service pushes — verifying each one before deploying and supervising the Java process. The Java
service runs the actual security tooling; the agent only owns the device identity and the update channel.

## Install

```
pip install .
```

Requires Python 3.9+. Depends on `click`, `requests`, `cryptography`, `websocket-client`, `coolname`.

## Use

```
simplit-board register     # sign in as an operator, pick where the board goes, enroll this device
simplit-board up           # hold the presence channel; receive -> verify -> deploy signed pushes
simplit-board status       # show local identity + deployed service version
```

### Register

`register` generates the device's name + Ed25519 identity (once, persisted), then signs you in as an operator
and enrolls the device. You are asked **where the board should go** — the organization root, or one of your
subdivisions:

```
simplit-board register
simplit-board register --email you@example.com --password '…' --subdivision "Buenos Aires HQ"
```

Your operator account must hold the `enrollDevice` permission, and CREATE on the location you pick — enrollment
and placement are authorized by the permission engine, not by the client.

### Up

`up` brings the device online and waits. It receives the operator's signed push over the presence relay,
verifies the signature, installs the Java service from the streamed bytes, and hands the presence session to
it. From then on the board service owns the channel and receives its own updates directly.

## Configuration

Everything is environment-overridable so the same agent runs against any environment without a rebuild:

| Variable | Default | Purpose |
|---|---|---|
| `SIMPLIT_STATE_DIR` | `/var/lib/simplit` | where the device identity + credential are stored |
| `SIMPLIT_DOMAIN` | the live cloud | base domain for the cloud services |
| `SIMPLIT_ORG` | `simplit` | the organization |

Identity is generated once and written atomically, so a power cut never mints a new device on reboot.
