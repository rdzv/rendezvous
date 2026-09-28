"""Build all portable runtimes concurrently in target-platform containers."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import urllib.request
import urllib.error

from release_config import (ROOT, VERSION, BUILD, RELEASE, ORIGIN, PLATFORMS,
                            ALPINE_IMAGE, UBUNTU_IMAGE, bundle_path, container_cli)


def run(command, timeout=900, **kwargs):
    return subprocess.run(command, check=True, timeout=timeout, **kwargs)


def is_published():
    request = urllib.request.Request(f'{ORIGIN}/releases/{VERSION}/manifest.json',
                                    headers={'User-Agent': 'Rendezvous-Release-Builder'})
    try:
        # Publisher origin and version are checked-in maintainer inputs.
        with urllib.request.urlopen(request, timeout=30):  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            return True
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        return False


def require_unpublished():
    if is_published():
        raise RuntimeError('Refusing to overwrite a published release; increment the version')


def build_platform(arch):
    config = PLATFORMS[arch]
    work = BUILD / arch
    bundle = bundle_path(arch)
    work.mkdir(parents=True, exist_ok=True)
    python_archive = work / 'python.tar.gz'
    if not python_archive.exists():
        temporary = python_archive.with_suffix('.download')
        # Checked-in HTTPS URL; the downloaded archive must match the pinned SHA-256.
        with urllib.request.urlopen(config['python_url'], timeout=120) as response, temporary.open('wb') as dest:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            shutil.copyfileobj(response, dest)
        temporary.replace(python_archive)
    if hashlib.sha256(python_archive.read_bytes()).hexdigest() != config['python_sha256']:
        raise RuntimeError(f'{arch}: upstream Python checksum mismatch')
    # Build scratch only. Never merge a new runtime with an old bundle.
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir()
    with tarfile.open(python_archive) as archive:
        archive.extractall(bundle, filter='data')
    run(['docker', 'run', '--rm', '--platform', config['docker'],
         '--mount', f'type=bind,src={bundle},dst=/out',
         '--mount', f'type=bind,src={ROOT}/tools/bundle_tor.sh,dst=/build.sh,readonly',
         '-e', f'OUT_UID={os.getuid()}', '-e', f'OUT_GID={os.getgid()}',
         ALPINE_IMAGE, 'sh', '/build.sh'])
    image = f'rendezvous-builder:{arch}'
    run(['docker', 'build', '--platform', config['docker'], '--build-arg', f'BASE_IMAGE={UBUNTU_IMAGE}',
         '-f', str(ROOT / 'tools/portable-build.Dockerfile'), '-t', image, str(ROOT / 'tools')])
    run(['docker', 'run', '--rm', '--platform', config['docker'],
         '--user', f'{os.getuid()}:{os.getgid()}', '-e', 'HOME=/tmp', '-e', f'RDZV_ARCH={arch}',
         '--mount', f'type=bind,src={bundle},dst=/out',
         '--mount', f'type=bind,src={ROOT},dst=/src,readonly',
         image, 'sh', '/src/tools/install_bundle_dependencies.sh'], timeout=1800)
    (bundle / 'app').mkdir()
    for name in ['rendezvous.py', 'agent_session.py']:
        shutil.copy2(ROOT / name, bundle / 'app' / name)
    (bundle / 'bin').mkdir()
    (bundle / 'bin/rdzv').write_text('''#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PATH="$ROOT/bin:$PATH"
exec "$ROOT/python/bin/python3" -I "$ROOT/app/rendezvous.py" "$@"
''')
    musl_arch = 'armhf' if arch == 'armv7l' else arch
    (bundle / 'bin/tor').write_text('''#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$ROOT/tor/lib/ld-musl-''' + musl_arch + '''.so.1" --library-path "$ROOT/tor/lib" "$ROOT/tor/bin/tor" "$@"
''')
    for name in ['rdzv', 'tor']:
        (bundle / 'bin' / name).chmod(0o755)
    result = run([*container_cli(arch), '--version'], capture_output=True, text=True)
    if result.stdout.strip() != f'Rendezvous {VERSION}':
        raise RuntimeError('CLI version does not match pyproject.toml')
    run([*container_cli(arch)[:-1], '/bundle/bin/tor', '--version'])
    (bundle / 'BUILD.json').write_text(json.dumps({
        'version': VERSION, 'platform': 'linux-' + arch, 'minimum_glibc': '2.34',
        'python_url': config['python_url'], 'python_sha256': config['python_sha256'],
        'alpine_image': ALPINE_IMAGE, 'build_image': UBUNTU_IMAGE, 'tor_package': '0.4.9.13-r0',
    }, indent=2) + '\n')
    archive = work / f'rendezvous-{VERSION}-linux-{arch}.tar.gz'
    with archive.open('wb') as raw, gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w|') as output:
            for file in sorted(bundle.rglob('*')):
                relative = file.relative_to(bundle)
                if '__pycache__' in relative.parts or file.suffix == '.pyc':
                    continue
                info = output.gettarinfo(str(file), str(relative))
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                info.mtime = 0
                if file.is_file() and not file.is_symlink():
                    with file.open('rb') as source:
                        output.addfile(info, source)
                else:
                    output.addfile(info)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    parts = []
    with archive.open('rb') as source:
        while data := source.read(16 * 1024 * 1024):
            name = archive.name + f'.part{len(parts):02d}'
            (RELEASE / name).write_bytes(data)
            parts.append(name)
    manifest = dict(version=VERSION, platform='linux-' + arch, minimum_glibc='2.34',
                    archive=archive.name, sha256=digest, bytes=archive.stat().st_size, parts=parts)
    (RELEASE / f'manifest-linux-{arch}.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Built {arch}: {digest}', flush=True)
    return arch, manifest


def build(replace=False):
    require_unpublished()
    RELEASE.mkdir(parents=True, exist_ok=True)
    if (RELEASE / 'manifest.json').exists() and not replace:
        raise RuntimeError('Staged release exists; use --replace-unpublished to rebuild it')
    # Incomplete builds must not retain a prior complete aggregate manifest.
    (RELEASE / 'manifest.json').unlink(missing_ok=True)
    with ThreadPoolExecutor(max_workers=len(PLATFORMS)) as workers:
        platforms = dict(workers.map(build_platform, PLATFORMS))
    manifest = dict(version=VERSION, platforms=platforms)
    (RELEASE / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (RELEASE / 'SHA256SUMS').write_text(''.join(f'{m["sha256"]}  {m["archive"]}\n' for m in platforms.values()))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replace-unpublished', action='store_true')
    build(parser.parse_args().replace_unpublished)
