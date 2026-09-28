"""Publish source and packaging recipes for the redistributed GPL/LGPL components."""
import hashlib
import json
from pathlib import Path
import re
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
from release_config import RELEASE
OUT = RELEASE / 'third-party'
OUT.mkdir(parents=True, exist_ok=True)
packages = [
    ('tor', 'community/tor', '0.4.9.13', 'https://dist.torproject.org/tor-0.4.9.13.tar.gz'),
    ('libseccomp', 'main/libseccomp', '2.6.0', 'https://github.com/seccomp/libseccomp/releases/download/v2.6.0/libseccomp-2.6.0.tar.gz'),
]
for name, directory, version, url in packages:
    # Public metadata in the fixed upstream repository; no maintainer GitHub login needed.
    request = urllib.request.Request(f'https://api.github.com/repos/alpinelinux/aports/contents/{directory}?ref=3.22-stable',
                                     headers={'User-Agent': 'Rendezvous-Release-Builder'})
    with urllib.request.urlopen(request, timeout=30) as response:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        files = json.load(response)
    recipes = OUT / (name + '-alpine-recipe')
    recipes.mkdir(exist_ok=True)
    for item in files:
        if item['type'] != 'file':
            continue
        if not item['download_url'].startswith('https://raw.githubusercontent.com/alpinelinux/aports/'):
            raise RuntimeError('Unexpected Alpine recipe download origin')
        # Only HTTPS files in the specific upstream packaging repository are accepted.
        with urllib.request.urlopen(item['download_url'], timeout=30) as response:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            (recipes / item['name']).write_bytes(response.read())
    recipe = (recipes / 'APKBUILD').read_text()
    if f'pkgver={version}' not in recipe:
        raise SystemExit('Alpine recipe no longer matches bundled version: ' + name)
    filename = url.rsplit('/', 1)[1]
    expected = re.search(r'([a-f0-9]{128})\s+' + re.escape(filename), recipe)
    if expected is None:
        raise SystemExit('Cannot find upstream source hash for ' + filename)
    target = OUT / filename
    if not target.exists():
        # Fixed upstream HTTPS source URL, checked against the packaging recipe's SHA-512.
        with urllib.request.urlopen(url, timeout=90) as response:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            target.write_bytes(response.read())
    if hashlib.sha512(target.read_bytes()).hexdigest() != expected[1]:
        raise SystemExit('Source checksum mismatch: ' + filename)
    with tarfile.open(target) as source:
        for member in source.getmembers():
            if member.isfile() and Path(member.name).name in ['LICENSE', 'COPYING', 'COPYING.LESSER'] and member.name.count('/') == 1:
                (OUT / (name + '-' + Path(member.name).name + '.txt')).write_bytes(source.extractfile(member).read())
    print('Verified source and Alpine build recipe:', filename)
(OUT / 'README.txt').write_text('Source and Alpine 3.22 build recipes for bundled Tor 0.4.9.13 and libseccomp 2.6.0.\nSource archives verified against recipe SHA-512 hashes. Other dependency metadata and Python license files are included in the portable bundle.\n')
