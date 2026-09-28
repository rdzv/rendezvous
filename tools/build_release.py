"""Generate the installer, existing website output, and release source archive."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import io
import json
import hashlib
import re
import tarfile

from build_cli_docs import render
from build_readme import render as render_readme
from build_portable import require_unpublished
from release_config import ROOT, VERSION, RELEASE, PLATFORMS, bundle_path, container_cli


def verified_manifest():
    manifest = json.loads((RELEASE / 'manifest.json').read_text())
    if manifest['version'] != VERSION or set(manifest['platforms']) != set(PLATFORMS):
        raise RuntimeError('Release manifest is incomplete or has the wrong version')
    for arch, entry in manifest['platforms'].items():
        expected_archive = f'rendezvous-{VERSION}-linux-{arch}.tar.gz'
        if entry['archive'] != expected_archive or entry['platform'] != 'linux-' + arch:
            raise RuntimeError('Unexpected platform archive name')
        digest = hashlib.sha256()
        size = 0
        for index, name in enumerate(entry['parts']):
            if name != expected_archive + f'.part{index:02d}':
                raise RuntimeError('Unexpected release part name')
            content = (RELEASE / name).read_bytes()
            digest.update(content)
            size += len(content)
        if digest.hexdigest() != entry['sha256'] or size != entry['bytes']:
            raise RuntimeError(f'{arch}: portable release parts do not match manifest')
        for name in ['rendezvous.py', 'agent_session.py']:
            if (bundle_path(arch) / 'app' / name).read_bytes() != (ROOT / name).read_bytes():
                raise RuntimeError(f'{arch}: built application is stale: {name}')
    return manifest


def installer_text(manifest):
    cases = []
    for arch, entry in manifest['platforms'].items():
        cases.append(f"    {arch})\n      RELEASE_SHA256='{entry['sha256']}'\n"
                     f"      RELEASE_PARTS='{' '.join(entry['parts'])}' ;; ")
    return ((ROOT / 'install.sh.in').read_text().replace('__VERSION__', VERSION)
            .replace('__PLATFORM_RELEASES__', '\n'.join(cases)))


def website():
    manifest = verified_manifest()
    # Documentation is extracted from each actual built launcher, not the source
    # checkout under the maintainer's Python. All platforms must agree.
    with ThreadPoolExecutor(max_workers=len(PLATFORMS)) as workers:
        pages = list(workers.map(lambda arch: render(None, ROOT, command=container_cli(arch)), PLATFORMS))
    if any(page != pages[0] for page in pages):
        raise RuntimeError('CLI documentation differs between platform bundles')
    (ROOT / 'public/cli.html').write_text(pages[0])
    (ROOT / 'public/install.sh').write_text(installer_text(manifest))
    # Only existing release numbers/links in the hand-authored pages are updated.
    for name in ['index.html', 'docs.html', 'security.html']:
        path = ROOT / 'public' / name
        content = path.read_text()
        content = re.sub(r'(?<=/releases/)\d+\.\d+\.\d+(?=/)', VERSION, content)
        content = re.sub(r'(?<=rendezvous-)\d+\.\d+\.\d+(?=-source\.tar\.gz)', VERSION, content)
        content = re.sub(r'(?<=LINUX PREVIEW · v)\d+\.\d+\.\d+', VERSION, content)
        content = re.sub(r'(?<=LINUX PREVIEW · )\d+\.\d+\.\d+', VERSION, content)
        content = re.sub(r'(?<=Downloading Rendezvous )\d+\.\d+\.\d+', VERSION, content)
        path.write_text(content)
    (ROOT / 'README.md').write_text(render_readme((ROOT / 'public/docs.html').read_text()))
    print('Built website and installer from all three verified platform bundles', flush=True)


def source_archive():
    require_unpublished()
    files = set()
    for pattern in ['*.py', '*.md', '*.toml', '*.lock', '.gitignore', '.semgrepignore', 'install.sh.in', 'package*.json',
                    'tools/*.py', 'tools/*.sh', 'tools/*.Dockerfile', 'tools/*.lock',
                    'test/*.py', 'test/*.sh', 'test/*.Dockerfile', 'test/web/*.js',
                    'web/*.js', 'public/*.html', 'public/*.css', 'public/*.js', 'public/*.svg']:
        files.update(ROOT.glob(pattern))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as output:
        for path in sorted(files):
            content = path.read_bytes()
            entry = tarfile.TarInfo(str(path.relative_to(ROOT)))
            entry.size = len(content)
            entry.mode = 0o644
            entry.mtime = 0
            output.addfile(entry, io.BytesIO(content))
    archive = RELEASE / f'rendezvous-{VERSION}-source.tar.gz'
    archive.write_bytes(gzip.compress(buffer.getvalue(), mtime=0))
    print('Source release:', archive)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--website-only', action='store_true')
    args = parser.parse_args()
    website()
    if not args.website_only:
        source_archive()
