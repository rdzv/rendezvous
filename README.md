# Rendezvous

Temporary shell access for agents and people. One invitation admits one SSH
connection over a client-authorized Tor onion service: an interactive shell for
people, or stateless command execution for agents.

## Run a session

On the host you want to share:

```sh
curl -fsSL https://host.rdzv.sh | sh
```

On the guest/agent's machine:

```sh
curl -fsSL https://join.rdzv.sh | sh
```

Use these same commands every time. Each invocation fetches the current
bootstrap, quietly reuses a matching cached runtime or downloads the new
release, and starts the session. No PATH setup or separate first-run workflow.

The host prints a cyan invitation, then `Waiting for single-use connection.`
It prints a green `Connected.` immediately on authentication. Background status
messages are muted. Piped output stays plain; `NO_COLOR` disables color.

For automation, save the invitation in an owner-only file:

```sh
curl -fsSL https://join.rdzv.sh | sh -s -- --invitation-file /private/path/invitation --command 'uname -a'
```

To join an interactive shell in one command:

```sh
curl -fsSL https://join.rdzv.sh | sh -s -- --invitation 'rv1.…'
```

Direct invitations appear in shell history and process arguments. Use
`--invitation-file` to keep the credential out of those locations; the options
are mutually exclusive. The bootstrap reconnects interactive input to the
terminal. With an explicit invitation and `--command`, bootstrap stdin is EOF.

For an agent whose tool calls use separate local shells:

```sh
curl -fsSL https://join.rdzv.sh | sh -s -- -a --invitation "<invitation>"
```

This starts one detached SSH client and prints exact session-specific commands
for `session exec`, `help`, and `close`. An exec call streams the command's stdout
and stderr directly and returns its exit code. Each command runs in a fresh shell
without a PTY; use `cd /path && ...` when needed. The private Unix socket accepts
only same-user peers. Invitations are delivered to the daemon through a pipe.
The controller exits when the session closes or reaches its lifetime limit.

The homepage Human / Agent switch copies this exact prompt:

> Join me on my terminal: `curl -fsSL https://join.rdzv.sh | sh -s -- -a --invitation "<invitation>"` with the following invitation:

Custom session limits:

```sh
curl -fsSL https://host.rdzv.sh | sh -s -- --invite-timeout 300 --session-timeout 1800
```

Defaults: ten minutes to redeem; twelve hours of access. Host and client each
support `--session-timeout SECONDS`; the earlier limit closes the connection.
The agent controller's limit includes startup and approval waiting time. An interactive
shell can run many commands; `--command` runs one command and ends the session.
Exit the remote shell or stop the host to end access. No reconnect is allowed.

The host logs complete SSH exec commands and interactive input in red, output
in cyan, and aligned neutral labels to stderr. NO_COLOR is honored.
Interactive input is a keystroke transcript, not reconstructed shell commands.
Typed input appends to one line until Enter. Outgoing PTY output (combined
stdout/stderr) follows logical line boundaries, with long lines shortened.
Recognized prompts and their typing echo are suppressed unless `host --verbose`
or `-v` is enabled. Guest data is unchanged; untrusted terminal controls are never
executed by the host logger. A persistent bottom bar shows `D: disconnect`, with
logs scrolling above it. Press uppercase `D` to disconnect without Enter;
Ctrl-C remains supported. The bar resizes and is cleaned up on exit.
The guest is notified before the host stops Tor. The generated `/cli` reference
lists every host/join option and is refreshed by `tools/build_release.py`.

## Command approval

```sh
curl -fsSL https://host.rdzv.sh | sh -s -- --approve
```

Approval is optional and off by default. It requires a host terminal and applies
to agent exec requests; interactive human sessions use a host without `--approve`.
The full command is logged before execution, followed by `Approve command?`.
While a decision is pending, the bottom bar shows exactly:

```text
a: approve, A: approve all, d: deny, D: disconnect
```

Only a/A are green and d/D red; the remaining text keeps its normal gray color.
`a` approves this request, `d` denies it with exit status 126, and `A` approves it
and all later requests in this session. Repeated unbound keys expand the prompt
to `Approve command (a/A/d)?`. Outside a pending decision only `D: disconnect`
is shown; lowercase d is not a disconnect key.

## Runtime and cache

The portable distribution includes Python, application dependencies, Tor, and
Tor's private loader/libraries. It runs as the invoking account and requires no
privileged installation. Platforms: Linux x86_64, aarch64 (ARM64), and armv7l
(32-bit ARM hard-float), with glibc 2.34+ and
base utilities curl, tar, sha256sum, getconf, and mktemp. Home must permit execution.

Verified bundles are cached under `~/.local/share/rendezvous`. Optional local
launchers in `~/.local/bin` permit offline startup but do not check for updates;
the website commands are the primary workflow. Ongoing sessions retain their
runtime when a new version is installed.

Public Tor directory data is reused between invocations to reduce bootstrap
downloads. Cache slots are exclusively leased so concurrent Tor processes do
not write the same files. Each session retains a fresh temporary DataDirectory,
service identity, client authorization, and SSH secret; these are never cached.
An unavailable/unsafe/full cache falls back to a private uncached session.

For a shorter host-side route:

```sh
curl -fsSL https://host.rdzv.sh | sh -s -- --single-hop
```

This opts into Tor's experimental non-anonymous single-onion mode: host-side
introduction/rendezvous circuits use one hop rather than three, giving up
host-location anonymity. Client routing and both access gates stay in place.
Default routing is unchanged. Network conditions still affect startup,
publication, and interactive RTT; a shorter circuit is not a latency guarantee.

The host subscribes to Tor descriptor events before enabling its network and
issues the invitation after acknowledgments from two distinct directory relays.
Clients retry transport/handshake failures only before sending credentials,
bounded by invitation expiry and a three-minute connection deadline. Ambiguous
authentication failures are not automatically retried; the token may be consumed.

## Security model

The invitation includes the onion address, Tor client-authorization credential,
a random SSH authentication secret, the pinned host-key fingerprint, and expiry.
The first successful SSH authentication consumes the secret atomically. Later
attempts fail, including concurrent connections accepted before the winning
claim. Human mode allows one shell/exec channel. Agent mode uses a control channel
and serialized native SSH exec channels on that same authenticated connection;
PTY requests are rejected in agent mode. Forwarding and SFTP are disabled.

The Tor credential itself is not single-use; SSH enforces redemption independently.
A fresh host invocation generates new identities and credentials, so a restart
does not revive a consumed invitation. A stolen *unredeemed* invitation can race
the intended guest. There is no independent identity check. Optional local
approval gates submitted agent exec requests, before their shell process starts.

The guest receives the host account's full shell privileges and environment.
This is not a sandbox. Local root/the same OS user can inspect credentials.
Ending a session does not undo file changes or guarantee terminating deliberately
detached processes. The shell's process group is terminated during cleanup.

The bootstrap verifies a pinned bundle hash before extraction. The initial
installer is still a trust anchor; the release is not independently signed or
audited. Build provenance and dependency metadata ship with the bundle, and
application, packaging, Tor, and libseccomp sources are published.

## Source development and verification

Source development requires Python 3.11+, the pyproject dependencies, and Tor.
End-user installations supply their own runtime instead. Local tests use pytest
and pytest-asyncio. `RUN_INSTALL_TEST=1` enables the local portable-bundle test;
`RUN_TOR_TEST=1` enables the original live Tor integration test.

`python3 tools/release.py publish` is the official build, verification, and
publication workflow. See `BUILD.md` for the target-platform containers, build
inputs, separate stages, and documentation generator ownership. `VERIFICATION.md`
records results. `DEPLOYMENT.md` describes Cloudflare infrastructure and account
boundaries.
