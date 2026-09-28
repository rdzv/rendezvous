# Official build and publication workflow

Use `python3 tools/release.py` from this directory. This is the release entry
point for maintainers and agents; do not substitute a direct Wrangler deployment
or assemble a release by remembering individual commands.

## Build machine

- Linux, Python 3.12+, Docker Engine with Buildx, Node.js/npm, GitHub CLI (`gh`), and Semgrep.
- Python test dependencies in `.venv`: `python3 -m venv .venv`, then
  `.venv/bin/python -m pip install -e '.[test]'`.
- Cloudflare deployment credentials in the environment, for **Fernando's account**
  specified by `wrangler.toml`. For the existing global key, use
  `CLOUDFLARE_API_KEY` and `CLOUDFLARE_EMAIL`. Do not put credentials in the source,
  build context, public assets, or command output.
  The release runner passes these credentials only to the Wrangler deployment
  subprocess, not to dependency installation, build tools, or tests.
- GitHub release access to `rdzv/rendezvous`, using `GH_TOKEN` or `GITHUB_TOKEN`
  (or an existing `gh` login). The runner passes GitHub token environment variables
  only to the GitHub publisher, separately from Cloudflare credentials.

On this x86_64 machine, enable the pinned ARM emulators once (and again after a
reboot if the registrations are gone):

```sh
python3 tools/release.py setup-emulation
```

This uses a privileged maintainer container to register QEMU for ARM64 and ARMv7.
Builds and tests then run actual target-architecture executables in Docker
containers. An ARM build machine is not required. Native ARM machines are useful
for hardware-specific testing and speed; emulation is functional verification,
not an ARM performance measurement. Native builders can use the same workflow
when Docker has execution support for all three target platforms.

## New runtime release

Set the new version in `pyproject.toml` and the CLI's `--version` in
`rendezvous.py`. The build checks that they agree. Versions in the installer,
release paths, CLI reference, and existing website version labels/source links
are derived from the release; do not update those generated values by hand.

Publication also requires an approved, pushed `v<version>` tag in
`rdzv/rendezvous`. The tag must contain the runtime application and dependency
declarations in the release source snapshot. Obtain authorization for commits,
tags, and pushes separately; the release uploader does not create or move tags.

```sh
python3 tools/release.py publish
```

This command performs the complete ordered workflow:

1. Reject an already-published runtime version.
2. Build x86_64, aarch64, and armv7l bundles concurrently in platform-specific
   containers. Each includes both host and guest.
3. Fetch the existing Tor/libseccomp source archives and packaging recipes,
   checking the upstream hashes.
4. Execute the **built launcher** for `--version` and every existing help section
   on **all three architectures**. Require identical output, then generate the
   CLI page, platform-selecting installer, website release references, README,
   and source archive. Before deployment, preflight the GitHub tag and artifacts.
5. Run the Python/web tests, Semgrep, and fresh/repeated rootless installation plus
   real Tor host/guest sessions on each architecture in read-only Ubuntu 22.04
   containers. These have no system Python, Tor, or sudo; the runtime is tested
   as UID 10001. Cross-architecture agent sessions also verify each platform as
   host and guest, including exact stdout/stderr and nonzero command exit status.
   Live Tor pairs run serially on the shared emulation host to avoid a burst of
   cold Tor clients from one egress address; runtime builds run concurrently.
   Explicit Tor bootstrap/publication/connection timeout errors are logged and
   retried with fresh sessions, at most three attempts. All other failures stop
   immediately. Each platform and cross-platform pair must pass its full checks;
   exhausting the retry limit blocks publication. Product timeouts are unchanged.
6. Deploy the website, Worker, installer, all three bundles, and other release
   assets together via the existing Wrangler configuration.
7. Check published pages against the built files, canonical host/join installers,
   manifests, and full archive checksums for all platforms. Repeat rootless
    installation from the public service on every architecture.
8. Upload the full runtime archives, original source snapshot, third-party
   sources/recipes/redistribution information, manifests,
   `provenance.json`, and `SHA256SUMS` to GitHub Releases. Upload to a draft,
   verify every asset, and then publish it. Existing assets must match exactly;
   the uploader never clobbers a different asset or mutates a published release.

Failures stop publication. A failed post-publication check is reported as a
failure, not as a successful release. Never reuse a published runtime version.

If Wrangler successfully published but a canonical endpoint briefly serves the
previous deployment during propagation, wait briefly and rerun the post-publication
checks without rebuilding or overwriting the published release:

```sh
python3 tools/check_deployment.py
python3 tools/check_deployment.py https://rendezvous.sh
python3 test/verify_platforms.py --published --install-only
```

All three commands must pass before calling the deployment verified.
For a new runtime release, complete/resume the GitHub upload with
`python3 tools/release.py github-release` after those checks pass.

For separate stages (for example while developing this workflow):

```sh
python3 tools/release.py build
python3 tools/release.py verify
python3 tools/release.py deploy
```

`deploy` includes the verification gates and refreshes the source snapshot only
for a not-yet-published release. To rebuild a staged release before it
has ever been published, use `build --replace-unpublished` or
`publish --replace-unpublished`. The remote manifest must return HTTP 404; a
network failure or any other response does not permit replacement.

If runtime compilation succeeded but a later asset step failed, run
`python3 tools/release.py assets` to resume source/website/installer generation
against the verified staged bundles, without recompiling them. This also refuses
to replace published release assets.

## Website-only updates and documentation ownership

**`public/docs.html` owns the user documentation, including README content.**
`tools/build_readme.py` converts that page's main content into `README.md`, with
absolute website links and a repository-specific link to these build instructions.
Edit the site documentation rather than maintaining a second copy in the README.
The website build regenerates the README, and verification rejects drift. For a
documentation-only edit, regenerate/check it directly with:

```sh
python3 tools/build_readme.py
python3 tools/build_readme.py --check
```

**`tools/build_cli_docs.py` owns the entire CLI reference page**, including its
surrounding documentation, navigation, and footer. Edit that generator when
changing the CLI documentation. `public/cli.html` is generated output; editing it
alone loses changes at the next build. Its template matches the website design.
Help text comes from running the packaged command, never from hand-maintained
option strings or from importing the checkout with the maintainer's Python.

The other existing pages/assets are maintained in `public/`; Worker routes are
in `web/`. To build and publish a website-only change using the current verified
bundles retained under `.build/<version>/`:

```sh
python3 tools/release.py website
python3 tools/release.py deploy
```

This does not rebuild or overwrite immutable release archives. A missing or
stale bundle is an error; do not silently fall back to generating help from the
source checkout. Keep release build artifacts available on the maintainer machine.

## GitHub releases

To mirror an already-deployed runtime or resume an interrupted upload:

```sh
python3 tools/release.py github-release --check
python3 tools/release.py github-release
```

Both commands require the existing pushed release tag. `--check` makes no GitHub
changes. Runtime archives are reconstructed from the verified Cloudflare parts,
so GitHub receives the identical full `.tar.gz` files rather than split downloads.
The original source archive remains unchanged. Publication checks its
`rendezvous.py`, `agent_session.py`, `requirements.lock`, and `pyproject.toml`
against the tagged GitHub commit, and checks the actual packaged application in
every runtime archive against that source snapshot.
The third-party source archive preserves the release's Tor/libseccomp sources,
packaging recipes, and redistribution information alongside the binaries.

`provenance.json` records this runtime-source match, the source commit, artifact
hashes, and the bundles' upstream component metadata. This is publisher-supplied
provenance; it does not claim the bundles were built by GitHub Actions or provide
a signed build attestation. `SHA256SUMS` covers all attached artifacts except
itself. GitHub Releases is the distribution mechanism for these archives.

For the first GitHub mirror of existing 0.2.8 assets, the source match is against
the original 0.2.8 repository commit (`e891625`), rather than a later repository
cleanup commit. Tagging/pushing that release still requires approval.

## Tests and repository layout

All Python tests, JavaScript tests, container fixtures, live-session verification,
and latency diagnostics live under `test/`. Pytest discovers `test/` by default;
`npm test` runs `test/web/*.test.js`. Build and publication code stays in `tools/`.
The obsolete root-based `test_clean_ubuntu.sh` fixture was replaced by the current
rootless installer tests and has been removed.

```sh
.venv/bin/python -m pytest -q
npm test
python3 test/verify_platforms.py
python3 test/verify_cross_platform.py
```

## Inputs and artifacts

- `tools/release_config.py`: target matrix, SHA-256-pinned standalone Python
  archives, and digest-pinned Alpine/Ubuntu/QEMU image references.
- `requirements.lock`: pinned runtime dependencies and upstream hashes.
- `tools/build-requirements.lock`: hash-pinned ARMv7 build tools. CFFI has no
  upstream ARMv7 wheel, so its pinned source is compiled in the ARMv7 container
  against libffi; that library and its license are bundled privately. Endpoints
  never compile, run pip, install system packages, or need administrator access.
- `tools/bundle_tor.sh`: pinned Tor package and architecture-correct private musl
  loader/libraries. All three endpoint platforms still require glibc 2.34+.
- `.build/<version>/<architecture>/`: downloaded interpreter, assembled runtime,
  and complete archive. Scratch directories may be rebuilt; published assets may not.
- `public/releases/<version>/manifest.json`: aggregate manifest with one entry
  per architecture; `manifest-linux-<architecture>.json` and `SHA256SUMS` describe
  individual archives. Archives are split into 16 MiB parts for Workers assets.
- `public/install.sh`: generated per-platform hash/part selection. It verifies the
  complete archive before extraction and keeps architecture-specific cache keys.
- `public/releases/<version>/rendezvous-<version>-source.tar.gz`: application,
   tests, build scripts/instructions, and website sources for that release.
- `.build/<version>/github-release/`: prepared full GitHub release artifacts,
  checksum inventory, and provenance record; not committed to Git.

The build pins Python, runtime dependencies, image inputs, and the Tor version.
Ubuntu build packages and Alpine transitive dependencies come from their signed
repositories, so this is a repeatable workflow, not a claim of bit-for-bit
reproducibility across repository changes. Each produced archive has its own
verified release hash. Dependency version changes are deliberate maintainer
operations, not part of endpoint installation.
