"""Exercise the actual POSIX installer against controlled release downloads."""
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tarfile
import re
import json
import pty
import sys

import pytest

ROOT = Path(__file__).parent
from tools.release_config import VERSION, PLATFORMS


def fixture_installer():
    cases = '\n'.join(f"    {arch}) RELEASE_SHA256='{'a' * 64}'; RELEASE_PARTS='fixture-{arch}.part00' ;;"
                      for arch in PLATFORMS)
    return ((ROOT / 'install.sh.in').read_text().replace('__VERSION__', VERSION)
            .replace('__PLATFORM_RELEASES__', cases))


@pytest.mark.parametrize('arguments,terminal', [
    (['--invitation', 'rv1.literal; $(never-run)'], True),
    (['--invitation=rv1.literal'], True),
    (['--invitation-file', '/private/invitation with spaces'], True),
    ([], True),
    (['--invitation', 'rv1.literal', '--command', 'uname -a'], False),
    (['--invitation-file=/private/code', '--command=uname -a'], False),
    (['--help'], False),
    (['-a', '--invitation', 'rv1.literal'], False),
    (['-a', '--session-timeout', '600', '--invitation', 'rv1.literal'], False),
    (['--agent', '--invitation-file=/private/code'], False),
])
def test_bootstrap_join_input_and_literal_arguments(tmp_path, arguments, terminal):
    home = tmp_path / 'home'
    root = home / '.local/share/rendezvous'
    template = fixture_installer()
    version = re.search(r"VERSION='([^']+)'", template)[1]
    target = root / (version + '-x86_64')
    target.mkdir(parents=True)
    runtime = root / 'runtime-test'
    (runtime / 'bin').mkdir(parents=True)
    (runtime / 'bin/rdzv').write_text(f'#!{sys.executable}\n' +
        'import json, os, sys\nprint(json.dumps(dict(args=sys.argv[1:], tty=os.isatty(0), '
        'input=sys.stdin.readline() if "--help" not in sys.argv else "")))\n')
    (runtime / 'bin/rdzv').chmod(0o700)
    (target / 'environment').write_text(str(runtime))
    (target / '.complete').write_text('a' * 64)
    script = template.replace('__RELEASE_SHA256__', 'a' * 64).replace('__DEFAULT_MODE__', 'join')
    env = dict(os.environ, HOME=str(home))
    master, slave = pty.openpty()
    try:
        if terminal:
            os.write(master, b'keyboard-input\n')
            # Establish a controlling terminal while stdin remains the bootstrap pipe.
            launcher = 'import os,fcntl,termios; os.setsid(); fcntl.ioctl(' + str(slave) + ',termios.TIOCSCTTY,0); os.execv("/bin/dash", ["dash", "-s", "--"] + __import__("sys").argv[1:])'
            result = subprocess.run([sys.executable, '-c', launcher, *arguments], input=script,
                capture_output=True, text=True, env=env, pass_fds=(slave,), timeout=10)
        else:
            result = subprocess.run(['dash', '-s', '--', *arguments], input=script,
                capture_output=True, text=True, env=env, start_new_session=True, timeout=10)
        assert result.returncode == 0, result.stderr
        record = json.loads(result.stdout)
        assert record['args'] == ['join', *arguments]
        assert record['tty'] is terminal
        assert record['input'] == ('keyboard-input\n' if terminal else '')
    finally:
        os.close(master)
        os.close(slave)


def test_invitation_options_are_mutually_exclusive():
    result = subprocess.run([sys.executable, str(ROOT / 'rendezvous.py'), 'join',
        '--invitation', 'rv1.test', '--invitation-file', '/unused'], capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert 'not allowed with argument' in result.stderr


def run_installer(tmp_path, archive, digest=None, arch='x86_64', system='Linux', bits=64):
    home = tmp_path / 'home'
    home.mkdir()
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    # Only downloads are substituted; the production extraction and verification run.
    (bin_dir / 'curl').write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$TEST_DOWNLOADS"\nwhile [ "$#" -gt 0 ]; do\n if [ "$1" = -o ]; then cp "$TEST_ARCHIVE" "$2"; exit; fi\n shift\ndone\nexit 1\n')
    (bin_dir / 'uname').write_text(f'#!/bin/sh\ncase "$1" in -m) printf "%s\\n" "{arch}" ;; -s) printf "%s\\n" "{system}" ;; esac\n')
    (bin_dir / 'getconf').write_text(f'#!/bin/sh\ncase "$1" in LONG_BIT) printf "%s\\n" "{bits}" ;; *) exec /usr/bin/getconf "$@" ;; esac\n')
    for file in bin_dir.iterdir():
        file.chmod(0o700)
    script = fixture_installer().replace('__DEFAULT_MODE__', 'install')
    if digest:
        script = re.sub(r"RELEASE_SHA256='[a-f0-9]+'", f"RELEASE_SHA256='{digest}'", script)
    env = dict(os.environ, HOME=str(home), PATH=str(bin_dir) + ':' + os.environ['PATH'],
               TEST_ARCHIVE=str(archive), TEST_DOWNLOADS=str(tmp_path / 'downloads'))
    return subprocess.run(['dash'], input=script, text=True, capture_output=True, env=env, timeout=90), home


@pytest.mark.parametrize('arch', PLATFORMS)
def test_corrupt_release_is_rejected(tmp_path, arch):
    archive = tmp_path / 'corrupt.tar.gz'
    archive.write_bytes(b'not the release')
    result, home = run_installer(tmp_path, archive, arch=arch)
    assert result.returncode != 0
    assert 'checksum mismatch' in result.stderr
    assert not (home / '.local/bin/rdzv').exists()


@pytest.mark.parametrize('arch', PLATFORMS)
def test_archive_path_escape_is_rejected(tmp_path, arch):
    archive = tmp_path / 'evil.tar.gz'
    with tarfile.open(archive, 'w:gz') as bundle:
        item = tarfile.TarInfo('../escape')
        item.size = 1
        bundle.addfile(item, io.BytesIO(b'x'))
    result, home = run_installer(tmp_path, archive, hashlib.sha256(archive.read_bytes()).hexdigest(), arch=arch)
    assert result.returncode != 0
    assert 'Unsafe release path' in result.stderr or 'Unexpected release contents' in result.stderr
    assert not (home / '.local/bin/rdzv').exists()


@pytest.mark.parametrize('machine,platform,bits', [('x86_64', 'x86_64', 64), ('aarch64', 'aarch64', 64),
    ('arm64', 'aarch64', 64), ('armv7l', 'armv7l', 32), ('armv8l', 'armv7l', 32), ('aarch64', 'armv7l', 32)])
def test_architecture_selects_download_and_cached_launcher(tmp_path, machine, platform, bits):
    archive = tmp_path / 'fixture.tar.gz'
    with tarfile.open(archive, 'w:gz') as bundle:
        for name in ['rdzv', 'tor']:
            content = f'#!/bin/sh\nprintf "%s\\n" "{platform}"\n'.encode()
            member = tarfile.TarInfo('bin/' + name)
            member.mode = 0o755
            member.size = len(content)
            bundle.addfile(member, io.BytesIO(content))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    result, home = run_installer(tmp_path, archive, digest, arch=machine, bits=bits)
    assert result.returncode == 0, result.stderr
    assert f'fixture-{platform}.part00' in (tmp_path / 'downloads').read_text()
    assert (home / f'.local/share/rendezvous/{VERSION}-{platform}/.complete').read_text().strip() == digest
    for alias in ['rdzv', 'rendezvous']:
        result = subprocess.run([str(home / '.local/bin' / alias), '--version'],
            env=dict(os.environ, HOME=str(home)), capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == platform


@pytest.mark.parametrize('system,arch', [('Linux', 'riscv64'), ('Linux', 'i686'), ('Darwin', 'aarch64')])
def test_unsupported_platform_stops_before_download(tmp_path, system, arch):
    result, home = run_installer(tmp_path, tmp_path / 'unused', arch=arch, system=system)
    assert result.returncode != 0
    assert not (tmp_path / 'downloads').exists()
    assert not (home / '.local').exists()


@pytest.mark.skipif(os.environ.get('RUN_INSTALL_TEST') != '1', reason='Requires locally built portable bundle')
def test_fresh_user_install(tmp_path):
    archive = ROOT / f'.build/{VERSION}/x86_64/rendezvous-{VERSION}-linux-x86_64.tar.gz'
    result, home = run_installer(tmp_path, archive, hashlib.sha256(archive.read_bytes()).hexdigest())
    assert result.returncode == 0, result.stderr
    assert result.stdout == ''
    assert f'Downloading Rendezvous {VERSION}' in result.stderr
    for unwanted in ['Installed:', 'export PATH', 'administrator access', 'self-contained']:
        assert unwanted not in result.stderr
    # The same bootstrap works on every invocation, quietly reusing its cache.
    cached = subprocess.run(['dash', '-s', '--', 'install'], input=(ROOT / 'public/install.sh').read_text(),
        text=True, capture_output=True, timeout=10,
        env=dict(os.environ, HOME=str(home), PATH=str(tmp_path / 'bin') + ':' + os.environ['PATH']))
    assert cached.returncode == 0, cached.stderr
    assert cached.stdout == '' and cached.stderr == ''
    for name in ['rdzv', 'rendezvous']:
        result = subprocess.run([str(home / '.local/bin' / name), '--version'], env=dict(os.environ, HOME=str(home)), text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == f'Rendezvous {VERSION}'
