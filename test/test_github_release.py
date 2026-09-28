import base64
import hashlib
import io
import json
import tarfile

import pytest

import publish_github as publisher


def tar_bytes(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


@pytest.fixture
def staged_release(tmp_path, monkeypatch):
    release = tmp_path / 'release'
    release.mkdir()
    sources = {'rendezvous.py': b'print("application")\n', 'agent_session.py': b'# agent\n',
               'requirements.lock': b'# pinned dependencies\n', 'pyproject.toml': b'[project]\nversion="0.2.8"\n'}
    source = release / 'rendezvous-0.2.8-source.tar.gz'
    source.write_bytes(tar_bytes(sources))
    third_party = release / 'third-party'
    third_party.mkdir()
    (third_party / 'README.txt').write_text('Corresponding third-party source information\n')
    (third_party / 'tor-0.4.9.13.tar.gz').write_bytes(b'upstream source archive fixture')
    files = {'app/' + name: sources[name] for name in ['rendezvous.py', 'agent_session.py']}
    files['BUILD.json'] = json.dumps({'version': '0.2.8', 'platform': 'linux-x86_64', 'tor_package': '0.4.9.13-r0'}).encode()
    archive = tar_bytes(files)
    filename = 'rendezvous-0.2.8-linux-x86_64.tar.gz'
    part = filename + '.part00'
    (release / part).write_bytes(archive)
    entry = dict(version='0.2.8', platform='linux-x86_64', archive=filename,
                 sha256=hashlib.sha256(archive).hexdigest(), bytes=len(archive), parts=[part])
    manifest = dict(version='0.2.8', platforms={'x86_64': entry})
    (release / 'manifest.json').write_text(json.dumps(manifest))
    (release / 'manifest-linux-x86_64.json').write_text(json.dumps(entry))
    monkeypatch.setattr(publisher, 'RELEASE', release)
    monkeypatch.setattr(publisher, 'BUILD', tmp_path / 'build')
    monkeypatch.setattr(publisher, 'verified_manifest', lambda: manifest)

    def reference(path, **kwargs):
        name = path.removeprefix('contents/').split('?')[0]
        return {'encoding': 'base64', 'content': base64.b64encode(sources[name]).decode()}

    monkeypatch.setattr(publisher, 'api', reference)
    return release, sources, files, manifest


def test_prepare_reassembles_archives_and_records_verified_source(staged_release):
    release, _, _, manifest = staged_release
    paths = publisher.prepare_assets('a' * 40)
    assets = {path.name: path for path in paths}
    entry = manifest['platforms']['x86_64']
    assert assets[entry['archive']].read_bytes() == (release / entry['parts'][0]).read_bytes()
    provenance = json.loads(assets['provenance.json'].read_text())
    assert provenance['runtime_source_commit'] == 'a' * 40
    assert provenance['artifacts'][entry['archive']]['sha256'] == entry['sha256']
    assert assets['rendezvous-0.2.8-source.tar.gz'].read_bytes() == (release / 'rendezvous-0.2.8-source.tar.gz').read_bytes()
    with tarfile.open(assets['rendezvous-0.2.8-third-party-sources.tar.gz']) as archive:
        assert archive.extractfile('third-party/tor-0.4.9.13.tar.gz').read() == b'upstream source archive fixture'
    for line in assets['SHA256SUMS'].read_text().splitlines():
        digest, name = line.split('  ', 1)
        assert publisher.digest(assets[name]) == digest


def test_reject_source_snapshot_that_does_not_match_tag(staged_release, monkeypatch):
    monkeypatch.setattr(publisher, 'api', lambda *args, **kwargs: {
        'encoding': 'base64', 'content': base64.b64encode(b'different source').decode()})
    with pytest.raises(RuntimeError, match='Tagged runtime source differs'):
        publisher.prepare_assets('a' * 40)


def test_reject_packaged_application_mismatch_even_with_a_valid_archive_hash(staged_release):
    release, _, files, manifest = staged_release
    files['app/rendezvous.py'] = b'print("different application")\n'
    data = tar_bytes(files)
    entry = manifest['platforms']['x86_64']
    (release / entry['parts'][0]).write_bytes(data)
    entry.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
    with pytest.raises(RuntimeError, match='packaged application differs'):
        publisher.prepare_assets('a' * 40)


def test_missing_tag_never_creates_or_pushes_a_tag(monkeypatch):
    calls = []

    def gh(*args, **kwargs):
        calls.append(args)
        return {'permissions': {'push': True}}

    monkeypatch.setattr(publisher, 'gh', gh)
    monkeypatch.setattr(publisher, 'api', lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match='no tag was created'):
        publisher.publish()
    assert all(call[0] == 'api' for call in calls)


def test_different_existing_asset_is_never_overwritten(tmp_path):
    asset = tmp_path / 'bundle.tar.gz'
    asset.write_bytes(b'new data')
    remote = {'assets': [{'name': asset.name, 'size': asset.stat().st_size, 'digest': 'sha256:' + '0' * 64}]}
    with pytest.raises(RuntimeError, match='Refusing to replace'):
        publisher.assert_existing_assets(remote, [asset])


@pytest.mark.parametrize('draft', [True, False])
def test_resume_existing_release_without_reuploading_matching_assets(tmp_path, monkeypatch, draft):
    asset = tmp_path / 'bundle.tar.gz'
    asset.write_bytes(b'correct data')
    release = {'draft': draft, 'assets': [{'name': asset.name, 'size': asset.stat().st_size,
                                         'digest': 'sha256:' + publisher.digest(asset)}]}
    calls = []
    monkeypatch.setattr(publisher, 'resolve_release_tag', lambda: 'a' * 40)
    monkeypatch.setattr(publisher, 'prepare_assets', lambda commit: [asset])
    monkeypatch.setattr(publisher, 'find_release', lambda: release)
    monkeypatch.setattr(publisher, 'gh', lambda *args, **kwargs: calls.append(args))
    publisher.publish()
    assert all(call[:2] == ('release', 'edit') for call in calls)
    assert len(calls) == int(draft)


def test_new_release_stays_draft_until_every_asset_is_verified(tmp_path, monkeypatch):
    asset = tmp_path / 'bundle.tar.gz'
    asset.write_bytes(b'correct data')
    output = tmp_path / 'build/github-release'
    output.mkdir(parents=True)
    monkeypatch.setattr(publisher, 'BUILD', tmp_path / 'build')
    monkeypatch.setattr(publisher, 'resolve_release_tag', lambda: 'a' * 40)
    monkeypatch.setattr(publisher, 'prepare_assets', lambda commit: [asset])
    release = None
    calls = []

    def gh(*args, **kwargs):
        nonlocal release
        calls.append(args)
        if args[:2] == ('release', 'create'):
            assert '--verify-tag' in args and '--draft' in args
            release = {'draft': True, 'assets': []}
        elif args[:2] == ('release', 'upload'):
            assert release['draft'] and '--clobber' not in args
            release['assets'] = [{'name': asset.name, 'size': asset.stat().st_size,
                                  'digest': 'sha256:' + publisher.digest(asset)}]
        elif args[:2] == ('release', 'edit'):
            assert len(release['assets']) == 1
            release['draft'] = False

    monkeypatch.setattr(publisher, 'gh', gh)
    monkeypatch.setattr(publisher, 'find_release', lambda: release)
    publisher.publish()
    assert [call[1] for call in calls] == ['create', 'upload', 'edit']
    assert release['draft'] is False


def test_missing_assets_in_an_already_published_release_are_not_mutated(tmp_path, monkeypatch):
    asset = tmp_path / 'bundle.tar.gz'
    asset.write_bytes(b'correct data')
    monkeypatch.setattr(publisher, 'resolve_release_tag', lambda: 'a' * 40)
    monkeypatch.setattr(publisher, 'prepare_assets', lambda commit: [asset])
    monkeypatch.setattr(publisher, 'find_release', lambda: {'draft': False, 'assets': []})
    monkeypatch.setattr(publisher, 'gh', lambda *args, **kwargs: pytest.fail('Unexpected GitHub mutation'))
    with pytest.raises(RuntimeError, match='refusing to mutate'):
        publisher.publish()


def test_release_lookup_finds_drafts_across_pages(monkeypatch):
    draft = {'tag_name': publisher.TAG, 'draft': True, 'assets': []}

    def gh(*args, **kwargs):
        assert args[0] == 'api' and '--paginate' in args and '--slurp' in args
        assert '/releases?' in args[-1]
        return [[{'tag_name': 'v0.1.0', 'draft': False}], [draft]]

    monkeypatch.setattr(publisher, 'gh', gh)
    assert publisher.find_release() == draft


def test_release_lookup_rejects_ambiguous_drafts(monkeypatch):
    draft = {'tag_name': publisher.TAG, 'draft': True, 'assets': []}
    monkeypatch.setattr(publisher, 'gh', lambda *args, **kwargs: [[draft, draft]])
    with pytest.raises(RuntimeError, match='Multiple GitHub releases'):
        publisher.find_release()
