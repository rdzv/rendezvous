# Cloudflare deployment

## Published now

- Worker: `rendezvous`
- Account: Fernando, `eb7e285239c6bee447d763904da41682`
- Website: https://rendezvous.fernando-eb7.workers.dev
- Host bootstrap: `/host`
- Join bootstrap: `/join`
- Install only: `/install`
- Current portable runtime: `/releases/0.2.8/manifest.json`, with x86_64, aarch64,
  and armv7l platform entries and archive parts.
- Application/build source: `/releases/0.2.8/rendezvous-0.2.8-source.tar.gz`.
- Older 0.1.1 and 0.2.x assets remain immutable historical downloads.

Cloudflare Workers Static Assets stores the site, installer, and release archive.
A small Worker selects routes and sets security headers. No D1, KV, R2, TURN,
session broker, or invitation database is provisioned. Worker observability is
disabled; Cloudflare still handles ordinary network/security metadata.

## Domain ownership and pending attachment

Jonathan owns `rendezvous.sh` and `rdzv.sh` in **his** Cloudflare account. No
duplicate zones were created in Fernando's account, and no domain DNS was edited.
Jonathan requires that the application remain entirely in Fernando's account.
Jonathan will only configure DNS in his own account. Do not request account
membership, deploy Workers into his account, or transfer his domains.

The appropriate cross-account design is Cloudflare for SaaS custom hostnames,
with the existing Worker as the provider-side origin. A CNAME to workers.dev
alone is not a substitute for custom-hostname onboarding and certificate issuance.

Provider zone `rdzv.net` is now active in Fernando's account, zone ID
`7babe7d120a264ed7e8adca7ec05ecc1`. Cloudflare for SaaS is enabled on its
$0/month base tier (100 included custom hostnames). Seven customer hostnames
are registered. The provider fallback origin is active.

- Customer CNAME target: `connect.rdzv.net` (proxied CNAME to `fallback.rdzv.net`).
- Fallback: `fallback.rdzv.net` (proxied originless A record, `192.0.2.1`).
- Worker routes: `connect.rdzv.net/*` and `fallback.rdzv.net/*`.
- Customer CNAMEs MUST be **Proxied** for provider-target routing (O2O).
- HTTPS website, installer content, same-origin release binding, and release
  integrity are verified directly against the provider endpoint.

The generic `*/*` route was rejected by Cloudflare with error 100327 even after
SaaS activation. Scoped CNAME-target routes are supported for this cross-account,
proxied-customer setup, per Cloudflare's hostname-routing documentation. Do not
change customer records to DNS-only without changing and verifying routing.

All eight hostnames are configured; both short installer endpoints respond over
HTTPS. See `dns/rendezvous.sh.zone`, `dns/rdzv.sh.zone`, and
`dns/README.md`. Certificates use automatic HTTP validation and renewal;
`_cf-custom-hostname` TXT records establish customer hostname ownership.

The intended customer-facing hostnames are:

| Hostname | Purpose |
| --- | --- |
| rendezvous.sh | Marketing, docs, security, releases |
| www.rendezvous.sh | Redirect to marketing apex |
| rdzv.sh | Redirect to rendezvous.sh, preserving paths |
| www.rdzv.sh | Redirect to marketing apex |
| host.rendezvous.sh | Compatibility redirect to host.rdzv.sh |
| join.rendezvous.sh | Compatibility redirect to join.rdzv.sh |
| host.rdzv.sh | Canonical host installer; `/install` installs only |
| join.rdzv.sh | Join installer |

Installers download their release from the same HTTPS origin that served them.
Canonical-domain installations do not depend on the workers.dev hostname;
all infrastructure remains in Fernando's account.

After provider-side SaaS configuration, Jonathan adds CNAME records to the
provider target in his own account and the hostname/certificate validation
records returned by Cloudflare. His Cloudflare DNS supports apex CNAME
flattening. His nameservers and domain ownership stay unchanged.

## Account boundary

No delegation into Jonathan's account is required or wanted. Infrastructure,
custom-hostname registration, and certificate provisioning belong in Fernando's
account; customer DNS changes belong to Jonathan.

References:
- https://developers.cloudflare.com/cloudflare-for-platforms/cloudflare-for-saas/start/getting-started/
- https://developers.cloudflare.com/cloudflare-for-platforms/cloudflare-for-saas/start/advanced-settings/worker-as-origin/
- https://developers.cloudflare.com/cloudflare-for-platforms/workers-for-platforms/configuration/hostname-routing/

## Build and verification

The official workflow and prerequisites are in [BUILD.md](BUILD.md). Use:

```sh
python3 tools/release.py publish
```

It builds all three architectures together, generates website/installer assets,
runs verification, deploys, and checks the published release. For website-only
changes, use `python3 tools/release.py website` followed by
`python3 tools/release.py deploy`. Do not bypass the workflow with direct Wrangler
commands. Both deployment paths stay in Fernando's existing account.

`tools/lock_dependencies.py` refreshes wheel hashes for explicitly selected
runtime versions. It is a maintainer operation, not a user install step.

`tools/build_cli_docs.py` is the source of truth for **all** of `public/cli.html`,
including surrounding documentation and navigation. Edit the generator, not the
generated page. The release build runs the packaged launcher on every architecture
for the existing general, host, join, and session help sections and requires
identical results. `test_cli_docs.py` rejects a stale public reference.

The workflow invokes `tools/build_portable.py` for new runtime versions. It uses
a digest-pinned Alpine build container for Tor and its private musl loader/libs,
and a SHA-256-pinned standalone Python distribution. Endpoint installation never
runs Docker, a package manager, or pip. The bundle is split into 16 MiB parts to
fit Workers Static Assets limits; the bootstrap checks the concatenated SHA-256
before extraction. `tools/vendor_tor_sources.py` publishes Tor/libseccomp source
and Alpine recipes, verifying upstream archives against recipe SHA-512 hashes.

Do not overwrite published `/releases/<version>/` archives: they are immutable
and cached long-term. Runtime changes require a new version and archive path.

Portable installer verification includes corrupt-archive/path-escape rejection,
fresh-user install, and real Tor command execution on Ubuntu 22.04 as UID 10001
with a read-only system filesystem and no system Python, Tor, or sudo. Both
aliases and repeat-install reuse passed. See tools/rootless-test.Dockerfile,
tools/test_rootless.sh, and VERIFICATION.md.
