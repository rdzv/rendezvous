"""Read live SaaS hostname validation data and export customer-owned DNS records."""
import json
import os
from pathlib import Path
import urllib.request

ZONE = '7babe7d120a264ed7e8adca7ec05ecc1'
request = urllib.request.Request(
    f'https://api.cloudflare.com/client/v4/zones/{ZONE}/custom_hostnames?per_page=50',
    headers={'X-Auth-Email': 'fernando@jdgregson.com', 'X-Auth-Key': os.environ['CLOUDFLARE_API_TOKEN']},
)
# Fixed Cloudflare HTTPS API and checked-in zone ID; no externally supplied URL.
with urllib.request.urlopen(request, timeout=30) as response:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
    result = json.load(response)
if not result.get('success'):
    raise SystemExit('Cannot retrieve SaaS hostnames')

out = Path(__file__).resolve().parents[1] / 'dns'
out.mkdir(exist_ok=True)
snapshot = out / 'hostname-status.json'
previous = {r['hostname']: r for r in json.loads(snapshot.read_text())} if snapshot.exists() else {}
for record in result['result']:
    # Cloudflare omits the ownership challenge once a hostname is verified.
    if 'ownership_verification' not in record:
        proof = previous.get(record['hostname'], {}).get('ownership_verification')
        if proof:
            record['ownership_verification'] = proof
for zone in ['rendezvous.sh', 'rdzv.sh']:
    records = sorted((r for r in result['result'] if r['hostname'] == zone or r['hostname'].endswith('.' + zone)), key=lambda r: r['hostname'])
    lines = [f'; Import only into Jonathan\'s {zone} zone.',
             '; Set ALL CNAME records to Proxied (orange cloud). TXT records are DNS-only.',
             '; Cloudflare for SaaS validates and renews HTTPS certificates automatically via HTTP.',
             f'$ORIGIN {zone}.', '$TTL 300', '']
    for record in records:
        hostname = record['hostname']
        label = '@' if hostname == zone else hostname[:-(len(zone) + 1)]
        proof = record.get('ownership_verification')
        lines.append(f'{label} IN CNAME connect.rdzv.net.')
        if proof:
            assert proof['type'] == 'txt' and proof['name'].endswith('.' + zone)
            proof_name = proof['name'][:-(len(zone) + 1)]
            lines.append(f'{proof_name} IN TXT "{proof["value"]}"')
        lines.append('')
        print(hostname, 'hostname=' + record['status'], 'certificate=' + record['ssl']['status'])
    (out / f'{zone}.zone').write_text('\n'.join(lines))
summary = [{k: r[k] for k in ['id', 'hostname', 'status', 'ownership_verification', 'ssl'] if k in r} for r in result['result']]
(out / 'hostname-status.json').write_text(json.dumps(summary, indent=2) + '\n')
print('DNS import files written to', out)
