# Rendezvous — project and agent handoff

## Repository documentation, tests, and GitHub publication

- Repository: `https://github.com/rdzv/rendezvous`; website: `https://rendezvous.sh`.
  GitHub's homepage field points to the website. All website footers link to the
  repository, including the CLI generator's footer.
- **README source of truth is `public/docs.html`.** `tools/build_readme.py`
  generates `README.md` from that page. Edit the website documentation and
  regenerate; do not maintain a separate README copy of the user instructions.
  The website build regenerates it and verification rejects drift.
- **CLI page source of truth remains `tools/build_cli_docs.py`.** Update the
  generator rather than `public/cli.html`.
- All tests, container fixtures, live verification, and latency diagnostics now
  live in `test/`; JavaScript tests are in `test/web/`. Build/deployment utilities
  remain in `tools/`. The obsolete root-based `test_clean_ubuntu.sh` is removed.
  Test paths in older verification history below refer to the previous layout.
- New runtime deployment preflights the existing pushed `v<version>` GitHub tag,
  then publishes verified full archives to GitHub Releases after Cloudflare checks.
  Resume/mirror with `python3 tools/release.py github-release`; add `--check` for
  read-only GitHub preflight. The uploader requires an approved existing tag,
  verifies the runtime source snapshot against it, uploads a draft, checks hashes,
  and publishes only after every asset is present. It never replaces a different
  asset. `provenance.json` is publisher metadata, not a signed Actions attestation.
- Existing 0.2.8 archives match runtime source in the original repository commit
  `e8916253a4404e9feea06ff76acde7dc61c31107`. Use that commit for its release tag,
  rather than retroactively rebuilding immutable 0.2.8 packages.
- See BUILD.md for credentials, publication ordering, and recovery commands.
  Git commits, tags, and pushes still require Jonathan's explicit approval.

## Latest iteration — 0.2.8 deployed and verified

- Deployment: `656b4490-e172-41c1-9d57-81ee87c386ec`. Canonical website,
  host/join installers, all three published archive hashes, source archive, and
  fresh/repeated public rootless installs verified. Published 0.2.8 assets are immutable.
- Verification: 87 Python tests passed (one legacy opt-in Tor test skipped), four
  web tests passed, Semgrep 229 rules with zero findings. Actual Tor sessions
  passed on all three architectures. Cross-architecture agent sessions passed
  x86_64 host → aarch64 guest, aarch64 host → armv7l guest, and armv7l host →
  x86_64 guest, including exact stdout/stderr, exit 17, remote CPU, and UID 10001.

- Official build/publish entry point: `python3 tools/release.py publish`. Read
  `BUILD.md` for prerequisites, separate stages, website-only deployment, and
  immutable-release rules. Do not invent a release command sequence or bypass
  verification with direct Wrangler deployment.
- Target matrix: Linux x86_64, aarch64 (ARM64), and armv7l (32-bit ARM hard-float),
  glibc 2.34+. Every bundle contains host and guest. The installer selects the
  platform's pinned archive and uses version-and-architecture cache keys.
- This x86_64 build host can execute ARM64 and ARMv7 containers via QEMU. No
  separate ARM machine is required for this workflow. Register emulators with
  `python3 tools/release.py setup-emulation` if needed after a reboot.
- **CLI documentation source of truth: `tools/build_cli_docs.py`.** It owns the
  complete CLI page template, including navigation/footer and explanatory text.
  Its template has been synchronized with the existing website. Change this
  generator, not `public/cli.html`; editing the generated page alone is lost.
  The release build extracts help by executing the actual built launcher on all
  architectures and checks that the outputs agree.
- Jonathan explicitly requires scoped changes: keep the existing website design
  and documentation intact except for requested changes, required platform/version
  accuracy, and generated command output. Ask before extending product scope.
- Earlier x86-only packaging notes and manual release commands below are historical.

Updated after the 0.2.8 multiarchitecture deployment.
Jonathan clarified that a request to continue from this handoff is sufficient
instruction to act on its outstanding work; do not ask him to repeat the task list.

## Previous iteration — 0.2.7 deployed and verified

**Connection reliability improvements** addressing Jonathan's report that connections
failed ~70% of the time:

- **Host side:** Now waits for **8 HSDir uploads** (full quorum: 2 replicas × 4
  HSDirs per replica) before issuing the invitation, instead of 2. This ensures
  the descriptor has propagated to all responsible directories before the client
  tries to connect. Previous behavior created a race where the client might query
  HSDirs that didn't have the descriptor yet.
- **Client side:** Faster failure, more retries:
  - Per-attempt SOCKS connect timeout: **15s** (was 45s)
  - SSH login timeout: **30s** (was 120s)
  - Retry sleep between attempts: **1s** (was 3s)
  This gives more connection attempts within the same 180-second deadline,
  increasing the chance of hitting a working route quickly.
- Verification: 36 Python tests passed, 4 web tests passed. `test_connection.py`
  updated to send 8 unique HSDir acknowledgments.
- Deployment: `fe69ea41-7f72-4b61-b2e1-68f6b0840222`.
  Bundle SHA-256: `caa865befb4b74f10032090bcbb1a7bb4c10ab7ed1665768352afb3b947c8a31`.
  Canonical installers verified. 0.2.7 is immutable.

## Previous iteration — 0.2.6 deployed and verified

**Product constraint from Jonathan:** do not add prompt-injection policies,
warnings, nonce fences, or JSON wrapping to returned command output. The tool
executes the command and returns its actual stdout, stderr, and exit code.
The old send/read and fenced-output behavior below is historical and superseded.

- Agent mode now exposes `session exec SESSION COMMAND`, plus `help` and `close`.
  One call streams separate stdout/stderr and returns the real command status.
  Each command runs in a fresh non-PTY shell. `cd` and shell variables do not
  persist; use `cd /path && ...`. One authenticated SSH connection stays alive.
  Native SSH exec channels are serialized; a dedicated control subsystem handles
  whole-session shutdown. Agent mode does not start a prompt-bearing shell.
- Human interactive PTY mode remains available. Optional `host --approve` is off
  by default and accepts agent command sessions only; interactive/PTY requests
  cannot bypass approval. It requires a controlling terminal.
- Approval UI is exactly:
  `a: approve, A: approve all, d: deny, D: disconnect`
  Show a/A/d only while waiting for a decision. Only a/A are green and d/D red;
  punctuation and descriptions stay the current gray. Outside a decision show
  only `D: disconnect`, with D red. Lowercase d no longer disconnects.
  Log the full command as `[input]`, then `Approve command? `. Echo the chosen key
  inline. After two unbound keys, append `Approve command (a/A/d)? `. a allows
  one request; d denies without execution (126); A allows current/subsequent
  requests. D and Ctrl-C end the session. Don't advertise Ctrl-C in the footer.
- Host/client defaults are 43,200 seconds (12 hours), adjustable with
  `--session-timeout SECONDS`; the earlier limit wins. The agent controller limit
  includes startup and approval waits, and it exits when SSH closes. No lingering
  output-retention process. Canceling a local exec terminates its remote job while
  preserving the connection for subsequent execs.
- Start fresh host and agent sessions for this protocol; old running cached
  runtimes are not updated in place. The exact website Agent prompt is unchanged.
- Verification: 73 Python tests, 4 web tests; Semgrep 219 rules, zero findings.
  Exact byte streams (including binary and >2 MiB output), exit codes, fresh shells,
  all approval outcomes, canceled pending requests, time limits, and human PTYs
  passed. Terminal emulator/real-PTY tests verify text, every key color, pending
  visibility, simple/escalated prompts, and cleanup.
  Final rootless real-Tor test through the piped host installer passed denial,
  single approval, allow-all, exact output/status 17, and fresh shells. D stopped
  the active exec in 1.048 s. `/tmp/opencode/rendezvous-026-exec-bundle.log`.
- Deployment: `803e0149-516d-484c-83f2-a715214bbe54`.
  Bundle SHA-256: `9eedce74b6ddad89973d3d67592beb48816c69a2dbddbababca30510e57e2322`.
  Canonical installers, exact published assets/help, downloaded hash, and fresh/
  repeated published rootless installation verified. 0.2.6 is immutable.
  No commits/pushes; local project still has no Git repository.

## Previous iteration — 0.2.5 deployed and verified

- **Agent mode is implemented:** `join -a` / `--agent` starts a detached process
  owning one SSH connection and one remote shell, then prints session-specific
  absolute-path commands. `session send/read/help/close` work from independent
  local shells. Working directory/environment/jobs persist remotely. No SSH
  reconnection or invitation reuse is introduced. `--command` and `-a` are exclusive.
- Implementation: `agent_session.py`, bundled next to `rendezvous.py` and loaded
  only from that exact application directory under isolated Python. Control socket
  lives under private `~/.local/share/rendezvous/sessions/<id>/control`, chmod 600,
  with SO_PEERCRED same-UID checks. Invitation passed over a pipe; no credential
  file/argv is used to start the worker. Buffer: 1 MiB / 4096 events, explicit
  overflow and monotonic cursors. Closed/failed established sessions retain output
  for up to fifteen minutes. Remote data/errors in reads are nonce-wrapped,
  JSON-escaped, and marked untrusted with the mandatory anti-bypass language.
- The website Human / Agent switch copies this exact prompt, including backticks;
  do not rewrite or expand it:

  > Join me on my terminal: `curl -fsSL https://join.rdzv.sh | sh -s -- -a --invitation "<invitation>"` with the following invitation:

- **Readiness/reliability:** host Tor starts with networking disabled, subscribes
  to HS_DESC via authenticated control, enables networking, and waits for uploads
  acknowledged by two distinct directories before issuing the invitation. Its
  lifetime starts afterward. SSH login timeout is 120 seconds. Clients reuse the
  same Tor process and retry only pre-credential transport/handshake failures,
  bounded by three minutes and invitation expiry. Never auto-retry an ambiguous
  authentication failure; the token may be consumed. Timeouts are stage-specific.
- **Host controls/logs:** D and d both work; stale Escape state expires so it cannot
  swallow a later key. Ctrl-C remains supported. Incoming content is red, outgoing
  cyan; neutral labels use an aligned 10-column prefix. ANSI character-set escapes
  (e.g. ESC ( B) are fully consumed in normal logging instead of leaking stray Bs.
- Verification: 63 Python tests, 4 web tests; 219 Semgrep rules, zero findings.
  Source and final rootless bundle passed real Tor agent sessions with independent
  CLI calls and persistent shell state. Final bundle publication: 14.56 s; host
  disconnect observed in 1.32 s, exit 130. Full piped host installer + lowercase d
  test disconnected its real Tor guest in 0.675 s and restored both terminals.
  Logs: `/tmp/opencode/rendezvous-025-agent-source.log`,
  `/tmp/opencode/rendezvous-025-agent-bundle.log`,
  `/tmp/opencode/rendezvous-025-host-key.log`.
- Deployment: `22930a71-431f-439e-994a-21db9f0a56b5`.
  Bundle hash: `952e4a02d9a89858a89c8b3a7c1534b6d7268dafe259c374c1f097998df3548a`.
  Canonical installers, exact deployed website assets/prompt, public help, downloaded
  bundle checksum, and fresh/repeated published rootless installation verified.
  Release 0.2.5 is immutable. No commits/pushes; directory still has no Git repo.

## Previous iteration — 0.2.4 deployed and verified

- Host input now appends in real time to the same `[input]` line until Enter;
  CRLF is treated as a single boundary. Output records follow logical newlines,
  not SSH chunks. Long normal output is shortened; verbose output is complete.
  Unterminated records are flushed before the session-end status.
- Normal logging strips terminal formatting/title sequences and suppresses
  recognized readline/bracketed-paste prompts plus their typing echo. Parsing
  survives arbitrarily split CSI/OSC sequences, including Jonathan's reported
  Bash prompt. Common plain prompts are also recognized. Verbose mode includes
  safely escaped prompts and controls. Guest bytes are unchanged.
- The startup hint is gone. A persistent bottom row contains **D: disconnect**;
  the log area scrolls above it. SIGWINCH adjusts the bar/scroll region, and
  cleanup restores normal scrolling and terminal attributes. Ctrl-C remains
  supported but is not advertised in the bar. Redirected logs get no bar codes.
- Verification: 46 Python tests, 3 web tests; terminal-emulator checks for scroll,
  wrapping, resize, hostile control text, and cleanup; controlling-PTY checks for
  D/Ctrl-C, SIGWINCH, piped stdin, and restoration. Semgrep 219 applicable rules,
  zero findings. `pyte==0.8.2` is a dev-only optional test dependency, not bundled.
- Published bundle SHA-256:
  `a864f0e85b2c1fa0873dbe37853c4d0112965f5e0afc42ed28cc49fcce64bcbb`.
  Deployment: `02ef92bd-ab8d-42de-9322-428c10ca7c8f`. Canonical installers,
  current CLI reference, complete public bundle hash, and fresh/repeated rootless
  installation verified. 0.2.4 is immutable; use a new version for runtime edits.

## Previous iteration — 0.2.3 deployed and verified

- Host console logging is on by default: complete SSH exec commands are labeled
  `[command]`; untruncated interactive input is labeled `[input]`. Long input is
  fragmented to bound buffering. This records keystrokes, not reconstructed shell
  commands after editing/history/expansion. Host logs escape terminal controls.
- Outgoing PTY data (combined stdout/stderr) is previewed on one line per chunk.
  `host --verbose` / `-v` logs the complete output. Guest bytes are unchanged.
- Uppercase **D** and **Ctrl-C** disconnect without Enter. Host controls use
  `/dev/tty` even with curl occupying stdin. Terminal settings are restored.
  Escape sequences ending in D (such as left arrow) do not trigger disconnect.
- Graceful shutdown sends `Session ended by host.` and SSH exit status 130, gives
  the guest up to five seconds to close SSH, and only then stops Tor. Tor now has
  its own OS session/process group: terminal SIGINT must not kill it before SSH
  can deliver the notice. Explicit cleanup and the owning-process monitor remain.
- **Approval mode was not added.** Jonathan explicitly permitted the logging
  fallback if reliable per-command approval could not fit the current SSH shell.
  Raw interactive SSH exposes keystrokes, not each executed command. Do not
  misrepresent this transcript as an approval/enforcement boundary.
- Public provider-deployment copy and frontend provider-command rewriting were
  removed. **https://rendezvous.sh/cli** shows actual general/host/join help.
  `tools/build_release.py` generates it from the CLI on every release build;
  `test_cli_docs.py` rejects stale content. Keep this release step mandatory.
- Verification: 37 Python tests, 3 web tests; Semgrep 219 applicable rules, zero
  findings. Real Tor host controls returned the joiner in **0.567 s (Ctrl-C)**
  and **0.557 s (D)** with notifications and both terminals restored. Evidence:
  `/tmp/opencode/rendezvous-023-controls-retry.log`. Earlier runs exposed the
  Tor SIGINT issue and one network connection failure. The final escape-sequence
  guard was subsequently tested with a controlling PTY before publication.
- Published release hash:
  `479efb0e6ec7442335e190a91098985ecfa507feb0cc70c97b8a8d519702c544`.
  Worker deployment: `dac1a9b0-fb06-4727-bc4a-7c5c4d044d41`. Canonical installers,
  public CLI/docs, full downloaded bundle hash, and fresh/repeated published
  rootless installation verified. Release 0.2.3 artifacts are now immutable.
- No commits or pushes; the local project still has no Git repository.

## Previous iteration — 0.2.2 deployed and verified

All three items from the previous handoff have been addressed:

1. **Terminal shutdown fixed.** AsyncSSH now owns duplicate descriptors, never the
   process-global standard streams. Terminal attributes and file-status flags
   (including O_NONBLOCK shared with the parent shell) are restored after cleanup.
   Actual interactive SSH/PTY regressions cover exit, SIGTERM, and connection loss.
2. **Direct invitations supported.**
   `curl -fsSL https://join.rdzv.sh | sh -s -- --invitation 'rv1.…'`
   is live. Mutually exclusive with `--invitation-file`. The bootstrap reconnects
   interactive input to `/dev/tty` even with an explicit invitation; explicit
   invitation plus `--command` uses EOF instead of forwarding installer text.
   Docs briefly explain shell-history/process-argument exposure.
3. **Tor cache and optional shorter routing.** Public directory data is cached in
   eight private, exclusively leased slots under
   `~/.local/share/rendezvous/tor-cache`. Concurrent Tor processes never use the
   same active slot. The lease is inherited by Tor to survive parent death until
   Tor exits. Unsafe/unavailable/full caches fall back to temporary storage.
   DataDirectory, service keys, authorization, cookies, and SSH secrets stay
   ephemeral. `host --single-hop` explicitly enables experimental non-anonymous
   single-onion mode; default routing is unchanged.

Use the shorter route with:
`curl -fsSL https://host.rdzv.sh | sh -s -- --single-hop`

**Verification:** 28 Python runtime/installer tests, 3 web tests; Semgrep 219
applicable rules, zero findings. Final bundle byte-matches the runtime source.
Three final rootless live interactive Tor sessions passed, including single-hop,
normal routing, warmed caches, terminal restoration, and exit status 23. Cache
inspection found only public Tor directory data and lease files.

Measured examples: single-hop median echo 567 ms; normal-route medians 1,369 ms
and 985 ms. Warm host-to-invitation times were 4.96 s and 4.78 s; an initial
empty-cache normal-route sample was 11.39 s. Samples are not a controlled speedup
estimate. One earlier Tor attempt timed out, so network variability remains.
Logs: `/tmp/opencode/rendezvous-022-final-tor.log` and
`/tmp/opencode/rendezvous-022-latency.log`. Runner: `tools/verify_latency.py`.
Use a fresh HOME for a genuinely cold cache; the final run reordered its cases,
so its second record's `cold` label actually used a populated cache.

**Published:** Worker deployment `33f04002-6cfd-41c7-8124-14d8294ccc35`.
Bundle SHA-256 `46e0fd08caf2818d5b324195390e01eec86f91f9fedcad58ee440ef9553c67fe`.
Canonical website and host/join installers, same-origin bindings, full downloaded
bundle hash, and fresh/repeated published rootless installation verified.
Release 0.2.2 artifacts are now immutable; do not rerun builders against them.

The local directory has **no Git repository**. No commits or pushes were made.
The `gh` executable was unavailable during this iteration; identical Tor and
libseccomp source artifacts/recipes were reused from 0.2.1 after SHA-512 checks.

## Product

On-demand, temporary terminal access for an agent or another person. The host
initiates access and shares a single-use invitation. Both endpoints connect
outbound through Tor; no inbound ports, bastion login, or manual SSH key setup.

Primary user experience, **every session**, including the first:

```sh
# Host
curl -fsSL https://host.rdzv.sh | sh

# Guest / agent
curl -fsSL https://join.rdzv.sh | sh
```

The bootstrap always fetches the current installer. It quietly reuses a matching
cached runtime or downloads the new release, then runs it. Cached local CLI
launchers are secondary, not the advertised workflow.

## Current state

- Repository/project directory: `/home/fernando/projects/rendezvous`.
- **Live release: 0.2.8.** Published to Cloudflare and verified on all three architectures.
- No commits or GitHub pushes were made during this work. Do not assume a public
  GitHub repository exists. Never commit/push without Jonathan's explicit request.
- The latest requested host logging, controls, graceful shutdown, and docs work is complete. See
  the latest-iteration summary above and historical context below.
- Website, bootstrap, and runtime are working; Jonathan has used real sessions.

## Product requirements / preferences

- Installation and operation must not require root, sudo, system package managers,
  or preinstalled Python/Tor.
- Same website command for first and subsequent sessions. No PATH setup guidance,
  install-location announcements, or alternate first-run instructions in normal output.
- Do not turn our discussion or previous implementation mistakes into permanent
  product copy. The download message is simply `Downloading Rendezvous 0.2.1…`.
- Market temporary shell access, not how short the name is. Removed the slogan
  “Four letters. One invitation.” Current heading: “Temporary access. On your terms.”
- Invitations are bold cyan, ordinary status muted, and Connected green on TTYs.
  Redirected output remains plain; honor `NO_COLOR`.
- Exact waiting text: **`Waiting for single-use connection.`**
- Show **`Connected.` immediately on authentication**, not after disconnect.
- Keep the public `/cli` help reference generated from the real parser on every
  release. Provider deployment details belong in internal deployment docs.
- Host commands belong in a persistent bottom status bar, currently only
  `D: disconnect`, not a one-time startup hint. Don't add a Ctrl-C reminder there.
- Log line boundaries must follow actual CR/LF, never network-read boundaries.
  Hide recognized prompts in normal mode; retain them in verbose mode.
- User-facing explanations should be direct and concise. Jonathan explicitly
  stopped this agent because it was consuming too much credit.
- Do not discuss IPv6 with Jonathan.

## Security and session semantics

- Temporary v3 onion service with X25519 client authorization.
- Loopback-only AsyncSSH listener with a fresh Ed25519 host key.
- Invitation contains onion address, Tor client credential, random 256-bit SSH
  secret, host-key fingerprint, and expiration.
- Client pins the SSH host key. No TOFU bypass.
- First successful SSH authentication atomically consumes the secret. One winning
  SSH connection, one shell/exec channel. No reconnection with that invitation.
- Listener closes after authentication. Existing pre-authentication connections
  still cannot redeem an already-consumed secret.
- Default enrollment lifetime: 600 seconds. Session lifetime: 3,600 seconds.
- No SFTP, TCP forwarding, agent forwarding, or persistent remote shell service.
- Stolen **unused** invitations can race the intended recipient. A consumed one
  cannot establish another connection. No separate approval prompt or identity check.
- Guest has invoking account privileges/environment. This is not a sandbox.
  Teardown does not undo filesystem changes or reliably kill deliberately
  detached descendants. Shell process-group cleanup is implemented.
- Website distributes code; it does not receive invitations or relay shell traffic.
- Initial bootstrap remains a trust anchor. Hash verification is not an independent
  publisher signature. Do not claim the product is audited or magically secure.

## Historical feedback — addressed in 0.2.2

The following records the original reports and investigation at the 0.2.1
handoff. The latest-iteration summary above describes the shipped fixes.

### 1. Fix joiner shutdown corrupting its terminal / closing standard streams

Jonathan sees this after exiting the remote shell:

```text
exit
object type name: ValueError
object repr     : ValueError('I/O operation on closed file')
lost sys.stderr
```

The output stair-steps across the screen, consistent with terminal raw mode not
being restored before an exception. **Fixed and PTY-tested in 0.2.2.**

Relevant code: `rendezvous.py`, `join()` (around lines 370–408 at handoff).
It passes these live process-global objects directly to AsyncSSH:

```python
stdin=ssh_stdin(sys.stdin.buffer)
stdout=sys.stdout.buffer
stderr=sys.stderr.buffer
```

The finally block calls `termios.tcsetattr(sys.stdin, ...)`. Likely cause:
AsyncSSH redirect cleanup closes a supplied standard stream; terminal restoration
then uses a closed object, and error reporting itself loses stderr. This hypothesis
has not yet been verified with an interactive PTY regression test.

Investigate redirect ownership; likely use owned duplicate descriptors/wrappers,
keep original standard streams open, and preserve a valid terminal descriptor for
restoration on every exit path. `dup()` shares file-status flags, so also consider
restoring any O_NONBLOCK changes. Test actual PTY interactive exit, not just
`--command` with pipes. Preserve remote output and exit status.

### 2. Support joining with an invitation in one command

Jonathan asked how to join without pasting the code interactively. In 0.2.1 only
interactive input and **`--invitation-file PATH`** exist. There is **no
`--invitation` argument in that release**. Added in 0.2.2.

The proposed straightforward UX was:

```sh
curl -fsSL https://join.rdzv.sh | sh -s -- --invitation 'rv1.…'
```

Implement the argument, make it mutually exclusive with `--invitation-file`, and
update installer input handling appropriately. Direct argument credentials are
visible in command history/process arguments; explain that tradeoff concisely.
Do not execute invitation text as shell code or introduce an unauthenticated
lookup service. Current documented noninteractive form is:

```sh
curl -fsSL https://join.rdzv.sh | sh -s -- --invitation-file /private/path/invitation --command 'uname -a'
```

Important: if a direct invitation is used for an **interactive remote shell**, the
curl pipeline occupies stdin. The installer still needs to reconnect shell input
to `/dev/tty`; do not simply skip TTY handling because the invitation is supplied.

### 3. Explain / investigate startup and interactive Tor latency

Jonathan reports:
- Approximately 1–2 seconds between typing and remote echo.
- Sometimes approximately 30 seconds from starting Tor until getting an invitation.

Current implementation creates a **fresh Tor DataDirectory every invocation** on
both host and client. It waits for the `Bootstrapped 100%` log before proceeding
(180-second timeout). This repeats directory/bootstrap/circuit work. Host prints
the invitation after bootstrap and reading hostname; descriptor publication may
still be propagating. Join has a further 120-second onion SOCKS-connect timeout.

The bootstrap delay and interactive RTT are different problems. There are no
intentional one-second keystroke delays in the relay code. Tor startup/network
variability also caused several live test timeouts before successful runs.

Research at the original handoff (single-hop and directory caching now implemented):
- Bundled Tor supports `HiddenServiceSingleHopMode` and
  `HiddenServiceNonAnonymousMode`. Experimental single-onion mode reduces the
  service-side circuit from three hops to one. It gives up service-location
  anonymity; client routing remains normal. SSH/authentication remain in place.
- Host already uses `SocksPort 0`, required for that mode. Each service uses a
  fresh service directory, also appropriate for switching modes.
- Jonathan says anonymity is not important to him. Nonetheless, the previous
  release did **not** silently change routing. Version 0.2.2 adds an explicit
  `--single-hop` option; normal routing remains the default.
- `EntryNodes` accepts relay fingerprints/countries; `MiddleNodes` is experimental.
  No turnkey geographically-nearest/lowest-latency selector. Country constraints
  require GeoIP data, not currently included in this bundle. ExitNodes is irrelevant
  to onion-service connections.
- Persisting only Tor network/bootstrap state separately from ephemeral service
  identities may help startup, but needs deliberate lifecycle/concurrency design.
  Do not accidentally preserve/revive old invitations or shared service keys.

Reference: https://manpages.debian.org/trixie/tor/tor.1.en.html

## Fixes already shipped in 0.2.1 — do not regress

- Removed the `await server.wait_closed()` immediately after closing the listener.
  Recent Python versions wait for accepted connections too; this delayed Connected
  and prevented the session deadline from starting until disconnect.
- Track accepted connections and close them at teardown; regression tests verify
  Connected arrives while the connection is still live and a deadline closes it.
- On normal completion, allow bounded time for SSH exit-status delivery before
  shutting down the underlying Tor process.
- `/dev/null` stdin maps to `asyncssh.DEVNULL`, avoiding an epoll PermissionError.
  Regular files and pipes keep normal input behavior.
- Quiet cache reuse, short output, TTY-aware colors, consistent website commands.

## Distribution / build

Portable bundles: Linux **x86_64, aarch64 (ARM64), and armv7l (32-bit ARM
hard-float), glibc 2.34+**. Both host and guest support every target. Musl hosts,
macOS, and Windows are not packaged.

- Python 3.12.14 from a digest-pinned Astral python-build-standalone artifact.
- AsyncSSH 2.24.0, cryptography 50.0.1, and dependencies from `requirements.lock`.
- Tor 0.4.9.13 from a signed Alpine package, with its own musl loader and libraries.
- Runtimes about 43 MB (x86_64), 39 MB (aarch64), and 35 MB (armv7l) compressed,
  each split into three parts (16 MiB maximum each)
  because Cloudflare Static Assets has a per-file size limit.
- Bootstrap concatenates parts, verifies complete SHA-256 before extraction,
  checks archive paths, then activates the per-user cache.
- Cache: `~/.local/share/rendezvous/<version>-<architecture>/environment` points at an immutable
  runtime directory. Optional launchers live in `~/.local/bin`.
- `.build/` and `public/releases/` are generated/ignored. Do not include `.venv`,
  node_modules, credentials, or private invitations in source artifacts.
- **Published release files are immutable. Use a new version for runtime changes.**

Key files:

| File | Purpose |
| --- | --- |
| `rendezvous.py` | Host/client, SSH authentication, PTY, Tor lifecycle |
| `BUILD.md`, `tools/release.py` | Official build, verification, and publication workflow |
| `tools/release_config.py` | Version from pyproject, architecture matrix, pinned build inputs |
| `tools/build_cli_docs.py` | Complete CLI page template; help extracted from built launchers |
| `install.sh.in` | Shell bootstrap template |
| `tools/build_portable.py` | Builds the bundled runtime and split artifacts |
| `tools/build_release.py` | Source archive and final checksum-bound installer |
| `tools/vendor_tor_sources.py` | Third-party source/recipes, with source hash verification |
| `tools/bundle_tor.sh` | Build-container-only package/library collection |
| `web/worker.js` | HTTP routing, installer mode/origin binding, security headers |
| `public/index.html`, `docs.html`, `security.html` | Published frontend |
| `test/test_rendezvous.py` | Real local SSH, replay/races, lifecycle, colors, stdin |
| `test/test_installer.py` | Archive integrity, path rejection, quiet install/cache |
| `test/test_rootless.sh` | Unprivileged/read-only-container validation |
| `test/verify_portable_session.py` | Real Tor session plus connection-status timing |
| `DEPLOYMENT.md`, `dns/` | Infrastructure, ownership boundaries, customer DNS |

For a new runtime release, update these two version declarations. The build
checks that they agree and derives installer, manifest, test, and website release
references automatically:

| File | What to change |
| --- | --- |
| `rendezvous.py` | `version='Rendezvous X.Y.Z'` in argparse |
| `pyproject.toml` | `version = "X.Y.Z"` |

The builder has `--replace-unpublished` for staging rebuilds; it checks that the
version's remote manifest is still 404.

## Build and deploy — exact steps

Read **BUILD.md** for prerequisites, emulation setup, separate build stages,
website-only changes, and post-publication checks. From the project directory,
use the existing Global API Key credentials and the official entry point:

```sh
source ~/fernando/config
export CLOUDFLARE_API_KEY="$CLOUDFLARE_API_TOKEN"
export CLOUDFLARE_EMAIL="fernando@jdgregson.com"
export GH_TOKEN="$GITHUB_PAT"
unset CLOUDFLARE_API_TOKEN
python3 tools/release.py publish
```

This builds all architectures concurrently, generates assets from the built
product, verifies, and publishes. Do not replace it with direct Wrangler calls.
For website-only changes, use `python3 tools/release.py website`, then
`python3 tools/release.py deploy`. CLI page edits belong in its generator.

In the 0.2.8 deployment, the first canonical installer check briefly saw the
previous deployment during propagation. Both canonical endpoints then matched
exactly. Follow the post-publication recovery commands in BUILD.md if this
occurs; do not rebuild or overwrite the already-published runtime.

## Cloudflare / domain ownership

**Jonathan's account:** owns `rendezvous.sh` and `rdzv.sh`. He changes their DNS.
**Fernando's account:** owns `rdzv.net` and all application infrastructure.
Do not request access to Jonathan's account, move domains, or deploy there.

- Account ID: `eb7e285239c6bee447d763904da41682`
- Provider zone ID: `7babe7d120a264ed7e8adca7ec05ecc1`
- Worker: `rendezvous`
- Provider CNAME target: `connect.rdzv.net`, proxied to `fallback.rdzv.net`
- Fallback: proxied originless A record `192.0.2.1`
- Worker routes: `connect.rdzv.net/*`, `fallback.rdzv.net/*`
- SaaS enabled: $0/month base, 100 included hostnames; eight configured.
- Customer CNAMEs **must be Proxied** for the current cross-account O2O target routes.
- Automatic HTTP certificate validation; ownership TXT records are in `dns/`.
- Cloudflare rejected `*/*` despite SaaS activation, so scoped target routes are used.

Canonical routes:
- `rendezvous.sh`: website.
- `rdzv.sh`, `www.rendezvous.sh`, `www.rdzv.sh`: redirects to canonical website.
- `host.rdzv.sh`, `join.rdzv.sh`: installer entrypoints.
- Long host/join names under rendezvous.sh: compatibility redirects to short ones.
- `https://rendezvous.fernando-eb7.workers.dev`: alternate provider deployment.

Secrets are in `/home/fernando/fernando/config`. Do not print them. Cloudflare
configuration uses the Global API Key stored as `CLOUDFLARE_API_TOKEN` (despite
that name), with email `fernando@jdgregson.com`, **not Bearer authentication**.
For Wrangler, export it as `CLOUDFLARE_API_KEY` and unset `CLOUDFLARE_API_TOKEN`.
See earlier deployment docs; all required resources already exist.

## Verification and workflow

Completed for published 0.2.1:
- 7 SSH/terminal/lifecycle tests + 3 installer tests + 3 web routing tests passed.
- Semgrep: 219 applicable rules, zero findings.
- Published bundle/hash and installer modes verified.
- Rootless Ubuntu 22.04 fixture, UID 10001, read-only system, all capabilities
  dropped, no system Python/Tor/sudo: fresh install and repeated install passed.
- A real Tor session returned remote UID 10001 and confirmed Connected was
  announced before client exit.
- **That live test used a command with piped stdout/devnull stdin, not an actual
  interactive terminal. It did not cover the newly reported shutdown bug.**

Run targeted tests first. Avoid repeatedly rebuilding/retesting unrelated pieces;
live Tor tests are slow and variable. Add an interactive PTY regression for the
new stream-ownership bug. Inspect `VERIFICATION.md` for prior evidence.

Use `system_run_steps` for multi-command sequences, bounded command tools for
individual execution, and `system_run_daemon` for long test processes. Do not
spawn subagents unless requested. Run Semgrep after code changes. Jonathan's
standing direction is to deploy requested Rendezvous changes and let him test.
Never claim a fix is live before publishing and verifying it.
