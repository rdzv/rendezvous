"""Maintainer-only: pin reviewed versions and all wheel hashes from PyPI metadata."""
import json
from pathlib import Path
import urllib.request

versions = {'asyncssh': '2.24.0', 'cryptography': '50.0.1', 'cffi': '2.1.1', 'pycparser': '3.0', 'typing-extensions': '4.16.0'}
lines = ['# Runtime dependencies. Binary wheels, except the pinned CFFI sdist built for ARMv7 by maintainers.']
for name, version in versions.items():
    # Fixed HTTPS registry; package names and versions are checked-in maintainer inputs.
    with urllib.request.urlopen(f'https://pypi.org/pypi/{name}/{version}/json', timeout=30) as response:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        metadata = json.load(response)
    hashes = sorted({item['digests']['sha256'] for item in metadata['urls']
                     if item['packagetype'] == 'bdist_wheel' or (name == 'cffi' and item['packagetype'] == 'sdist')})
    if not hashes:
        raise RuntimeError(f'No wheels for {name}=={version}')
    lines.append(f'{name}=={version} \\\n' + ' \\\n'.join('    --hash=sha256:' + digest for digest in hashes))
(Path(__file__).resolve().parents[1] / 'requirements.lock').write_text('\n'.join(lines) + '\n')
print('Locked', ', '.join(f'{name}=={version}' for name, version in versions.items()))
