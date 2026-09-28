"""Publish verified portable archives to an existing GitHub release tag."""
import argparse
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

from build_release import verified_manifest
from release_config import BUILD, RELEASE, VERSION, GITHUB_REPOSITORY

SOURCE_FILES = ('rendezvous.py', 'agent_session.py', 'requirements.lock', 'pyproject.toml')
TAG = 'v' + VERSION


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def gh(*arguments, json_result=False, missing_ok=False):
    environment = dict(os.environ, GH_HOST='github.com', GH_PROMPT_DISABLED='1')
    for name in ['CLOUDFLARE_API_KEY', 'CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_EMAIL']:
        environment.pop(name, None)
    result = subprocess.run(['gh', *arguments], capture_output=True, text=True,
                            env=environment, timeout=900)
    data = None
    if json_result:
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            pass
    if result.returncode:
        if missing_ok and isinstance(data, dict) and str(data.get('status')) == '404':
            return None
        raise RuntimeError(result.stderr.strip() or 'GitHub CLI request failed')
    if json_result and data is None:
        raise RuntimeError('GitHub returned an invalid JSON response')
    return data if json_result else result.stdout


def api(path, *, missing_ok=False):
    return gh('api', f'repos/{GITHUB_REPOSITORY}/{path}', json_result=True, missing_ok=missing_ok)


def find_release():
    # The release-by-tag REST endpoint omits drafts. Listing authenticated
    # releases is required to verify or resume an upload before publication.
    pages = gh('api', '--paginate', '--slurp',
               f'repos/{GITHUB_REPOSITORY}/releases?per_page=100', json_result=True)
    matches = [release for page in pages for release in page if release['tag_name'] == TAG]
    if len(matches) > 1:
        raise RuntimeError('Multiple GitHub releases use this tag; refusing to choose or overwrite one')
    return matches[0] if matches else None


def resolve_release_tag():
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?', VERSION):
        raise RuntimeError('Invalid release version')
    repository = gh('api', f'repos/{GITHUB_REPOSITORY}', json_result=True)
    if not repository.get('permissions', {}).get('push'):
        raise RuntimeError('GitHub token does not have repository write permission')
    reference = api(f'git/ref/tags/{TAG}', missing_ok=True)
    if reference is None:
        raise RuntimeError(f'Push an approved {TAG} release tag to {GITHUB_REPOSITORY} before publishing; no tag was created')
    obj = reference['object']
    for _ in range(8):
        if not re.fullmatch(r'[0-9a-f]{40}', obj['sha']):
            raise RuntimeError('Invalid Git object ID')
        if obj['type'] == 'commit':
            return obj['sha']
        if obj['type'] != 'tag':
            break
        obj = api('git/tags/' + obj['sha'])['object']
    raise RuntimeError('Release tag does not resolve to a commit')


def member_bytes(archive, name):
    member = archive.getmember(name)
    if not member.isfile() or member.size > 2 * 1024 * 1024:
        raise RuntimeError(f'Invalid source/metadata member: {name}')
    return archive.extractfile(member).read()


def third_party_sources(output, build_metadata):
    sources = RELEASE / 'third-party'
    if not (sources / 'README.txt').is_file():
        raise RuntimeError('Missing third-party source/redistribution information')
    for metadata in build_metadata.values():
        version = metadata['tor_package'].split('-r', 1)[0]
        if not re.fullmatch(r'[0-9.]+', version) or not (sources / f'tor-{version}.tar.gz').is_file():
            raise RuntimeError('Missing corresponding source for the bundled Tor version')
    target = output / f'rendezvous-{VERSION}-third-party-sources.tar.gz'
    with target.open('wb') as raw, gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w|') as archive:
            for path in sorted(sources.rglob('*')):
                if path.is_symlink():
                    raise RuntimeError('Unexpected symlink in third-party source assets')
                if not path.is_file():
                    continue
                member = tarfile.TarInfo(str(path.relative_to(RELEASE)))
                member.mode = 0o644
                member.size = path.stat().st_size
                member.mtime = 0
                with path.open('rb') as source:
                    archive.addfile(member, source)
    return target


def prepare_assets(commit):
    """Reassemble exactly the published/staged bytes; never rebuild old releases."""
    manifest = verified_manifest()
    source_name = f'rendezvous-{VERSION}-source.tar.gz'
    source_path = RELEASE / source_name
    with tarfile.open(source_path) as archive:
        sources = {name: member_bytes(archive, name) for name in SOURCE_FILES}
    for name, content in sources.items():
        reference = api(f'contents/{name}?ref={commit}')
        if reference.get('encoding') != 'base64' or base64.b64decode(reference['content']) != content:
            raise RuntimeError(f'Tagged runtime source differs from the release source snapshot: {name}')
    output = BUILD / 'github-release'
    output.mkdir(parents=True, exist_ok=True)
    assets = []
    build_metadata = {}
    for arch, entry in manifest['platforms'].items():
        target = output / entry['archive']
        with target.open('wb') as destination:
            for part in entry['parts']:
                with (RELEASE / part).open('rb') as source:
                    shutil.copyfileobj(source, destination)
        if target.stat().st_size != entry['bytes'] or digest(target) != entry['sha256']:
            raise RuntimeError(f'Archive changed during preparation: {arch}')
        with tarfile.open(target) as archive:
            for name in ['rendezvous.py', 'agent_session.py']:
                if member_bytes(archive, 'app/' + name) != sources[name]:
                    raise RuntimeError(f'{arch}: packaged application differs from the tagged source')
            metadata = json.loads(member_bytes(archive, 'BUILD.json'))
        if metadata['version'] != VERSION or metadata['platform'] != 'linux-' + arch:
            raise RuntimeError(f'{arch}: incorrect runtime build metadata')
        build_metadata[arch] = metadata
        assets.append(target)
    for name in [source_name, 'manifest.json', *[f'manifest-linux-{arch}.json' for arch in manifest['platforms']]]:
        target = output / name
        shutil.copyfile(RELEASE / name, target)
        assets.append(target)
    assets.append(third_party_sources(output, build_metadata))
    records = {path.name: {'sha256': digest(path), 'bytes': path.stat().st_size} for path in assets}
    provenance = {
        'schema_version': 1,
        'repository': 'https://github.com/' + GITHUB_REPOSITORY,
        'tag': TAG,
        'runtime_source_commit': commit,
        'source_verification': 'Runtime source snapshot and packaged application compared with the tagged GitHub commit',
        'runtime_source_sha256': {name: hashlib.sha256(content).hexdigest() for name, content in sources.items()},
        'build_metadata': build_metadata,
        'artifacts': records,
    }
    provenance_path = output / 'provenance.json'
    provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + '\n')
    assets.append(provenance_path)
    sums = output / 'SHA256SUMS'
    sums.write_text(''.join(f'{digest(path)}  {path.name}\n' for path in sorted(assets)))
    assets.append(sums)
    return assets


def assert_existing_assets(release, assets):
    """A published or partially uploaded asset is immutable, including on retries."""
    expected = {path.name: path for path in assets}
    existing = {asset['name']: asset for asset in release.get('assets', [])}
    for name in expected.keys() & existing.keys():
        local = expected[name]
        remote = existing[name]
        if remote['size'] != local.stat().st_size:
            raise RuntimeError(f'Refusing to replace different GitHub release asset: {name}')
        if remote.get('digest'):
            remote_digest = remote['digest']
        else:
            with tempfile.TemporaryDirectory(prefix='rdzv-release-check-') as directory:
                gh('release', 'download', TAG, '--repo', GITHUB_REPOSITORY,
                   '--pattern', name, '--dir', directory)
                remote_digest = 'sha256:' + digest(Path(directory) / name)
        if remote_digest != 'sha256:' + digest(local):
            raise RuntimeError(f'Refusing to replace different GitHub release asset: {name}')
    return [path for name, path in expected.items() if name not in existing]


def publish(*, check=False):
    commit = resolve_release_tag()
    assets = prepare_assets(commit)
    release = find_release()
    if release is not None:
        missing = assert_existing_assets(release, assets)
        if not release['draft'] and missing:
            raise RuntimeError('Published GitHub release is incomplete; refusing to mutate it')
    else:
        missing = assets
    if check:
        print(f'GitHub release preflight passed: {GITHUB_REPOSITORY} {TAG} -> {commit}; {len(assets)} verified assets')
        return
    if release is None:
        notes = BUILD / 'github-release/release-notes.md'
        notes.write_text(f'''Rendezvous {VERSION}

Website and documentation: https://rendezvous.sh

Portable Linux bundles for x86_64, aarch64 (ARM64), and armv7l (32-bit ARM hard-float), containing both host and guest. Requires glibc 2.34+.

These are the exact runtime bytes distributed by the website. The original release source snapshot is included unchanged. Its runtime application files and dependency declarations match [{commit}](https://github.com/{GITHUB_REPOSITORY}/commit/{commit}). Corresponding Tor/libseccomp sources, packaging recipes, and redistribution information are included in the third-party sources archive; third-party licenses remain with their components.

`SHA256SUMS` covers the attached artifacts. `provenance.json` records the verified runtime source commit, artifact digests, and bundled component metadata. This is publisher-supplied provenance, not a GitHub Actions build attestation.
''')
        gh('release', 'create', TAG, '--repo', GITHUB_REPOSITORY, '--verify-tag',
           '--draft', '--title', f'Rendezvous {VERSION}', '--notes-file', str(notes))
    if missing:
        gh('release', 'upload', TAG, '--repo', GITHUB_REPOSITORY, *map(str, missing))
    release = find_release()
    if release is None:
        raise RuntimeError('Uploaded GitHub draft release could not be found')
    if assert_existing_assets(release, assets):
        raise RuntimeError('GitHub upload verification found missing assets')
    if resolve_release_tag() != commit:
        raise RuntimeError('Release tag changed during publication')
    if release['draft']:
        gh('release', 'edit', TAG, '--repo', GITHUB_REPOSITORY, '--draft=false')
    print(f'Verified GitHub release: https://github.com/{GITHUB_REPOSITORY}/releases/tag/{TAG}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Validate the tag, source, and assets without changing GitHub')
    publish(check=parser.parse_args().check)
