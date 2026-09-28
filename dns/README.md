# Customer DNS setup

These files belong in **Jonathan's** Cloudflare account. The provider domain
`rdzv.net`, SaaS subscription, and Worker remain in **Fernando's** account.

For each domain, open DNS > Records > Import and Export > Import DNS records,
and import the matching `.zone` file. Select proxying for imported CNAME records
and verify all CNAME rows show **Proxied** (orange cloud); TXT rows are DNS-only.

The two essential traffic records are:

| Zone | Type | Name | Target | Proxy |
| --- | --- | --- | --- | --- |
| rendezvous.sh | CNAME | @ | connect.rdzv.net | Proxied |
| rdzv.sh | CNAME | @ | connect.rdzv.net | Proxied |

Each root also has a `_cf-custom-hostname` TXT ownership proof in its import file.
The files additionally configure `www`, `host`, and `join` aliases as appropriate.

If a matching hostname already has an address/alias record, inspect and replace
that conflicting record instead of creating a duplicate. Preserve unrelated
mail and other DNS records. No nameserver changes are needed.

Proxying is REQUIRED for these scoped provider-target Worker routes. Cloudflare
calls this cross-account routing O2O. DNS-only customer CNAMEs are not supported
by the current route configuration.

Certificates use automatic HTTP validation and renewal. The TXT records above
prove hostname ownership; they are not manual certificate-renewal tokens. The
provider will check both hostname and certificate status after DNS is configured.

`hostname-status.json` is a snapshot, not a live readiness indicator. It contains
public DNS verification values, not API credentials.
