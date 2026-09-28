"""Verify published pages, entrypoints, and every architecture's release bytes."""
import hashlib
import json
import sys
import urllib.request
import urllib.parse

from release_config import ROOT, VERSION, PLATFORMS, RELEASE, ORIGIN

BASE = sys.argv[1].rstrip('/') if len(sys.argv) > 1 else ORIGIN


def fetch(path, origin=BASE):
    if urllib.parse.urlsplit(origin).scheme != 'https' or not path.startswith('/'):
        raise ValueError('Deployment checks require an HTTPS origin and an absolute URL path')
    # Maintainer-specified HTTPS deployment origin; file/other URL schemes rejected above.
    return urllib.request.urlopen(urllib.request.Request(origin + path,  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        headers={'User-Agent': f'Rendezvous-Deployment-Check/{VERSION}'}), timeout=120)


for path, name in [('/', 'index.html'), ('/docs', 'docs.html'), ('/security', 'security.html'), ('/cli', 'cli.html')]:
    with fetch(path) as response:
        assert response.status == 200
        assert response.read() == (ROOT / 'public' / name).read_bytes(), f'Stale published page: {path}'
        assert response.headers['X-Content-Type-Options'] == 'nosniff'
        assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']
    print('OK', path)

template = (ROOT / 'public/install.sh').read_text()
for origin, path, mode in [(BASE, '/host', 'host'), (BASE, '/join', 'join'), (BASE, '/install', 'install'),
                           ('https://host.rdzv.sh', '/', 'host'), ('https://join.rdzv.sh', '/', 'join')]:
    with fetch(path, origin) as response:
        expected = template.replace('__DEFAULT_MODE__', mode).replace(ORIGIN, origin)
        assert response.read().decode() == expected, f'Stale installer: {origin}{path}'
        assert response.headers['Content-Type'].startswith('text/plain')
        assert response.headers['Cache-Control'] == 'no-store'
    print('OK', origin + path, mode)

prefix = f'/releases/{VERSION}/'
with fetch(prefix + 'manifest.json') as response:
    manifest = json.load(response)
assert manifest == json.loads((RELEASE / 'manifest.json').read_text())
assert set(manifest['platforms']) == set(PLATFORMS)
for arch, entry in manifest['platforms'].items():
    actual = hashlib.sha256()
    size = 0
    for name in entry['parts']:
        assert name.startswith(f'rendezvous-{VERSION}-linux-{arch}.tar.gz.part') and '/' not in name
        with fetch(prefix + name) as response:
            data = response.read()
        actual.update(data)
        size += len(data)
    assert actual.hexdigest() == entry['sha256'] and size == entry['bytes']
    assert entry['sha256'] in template
    print('OK', arch, 'published archive checksum:', actual.hexdigest())
for name in ['SHA256SUMS', f'rendezvous-{VERSION}-source.tar.gz', *[f'manifest-linux-{arch}.json' for arch in PLATFORMS]]:
    with fetch(prefix + name) as response:
        assert response.read() == (RELEASE / name).read_bytes(), f'Stale release asset: {name}'
    print('OK', name)
