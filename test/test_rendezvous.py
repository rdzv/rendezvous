import asyncio
import time
import secrets
import os
from contextlib import asynccontextmanager
from contextlib import ExitStack
from types import SimpleNamespace
import io
import fcntl
import json
from pathlib import Path
import pty
import signal
import socket
import subprocess
import sys
import termios

import asyncssh
import pytest

from rendezvous import Gate, Server, PinnedClient, shell_session, decode_invitation, encode_invitation
import rendezvous


def test_tor_cache_leases_isolate_concurrent_sessions_and_reuse_public_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home with spaces'))
    root = tmp_path / 'session'
    root.mkdir()
    with rendezvous.tor_cache(root) as (first, lease):
        assert first != root / 'cache'
        assert lease is not None
        (first / 'cached-microdesc-consensus').write_text('public directory information')
        with ExitStack() as stack:
            others = [stack.enter_context(rendezvous.tor_cache(root))[0] for _ in range(8)]
            assert len(set([first, *others])) == 9
            assert others[-1] == root / 'cache'
            assert not any((other / 'cached-microdesc-consensus').exists() for other in others)
    with rendezvous.tor_cache(root) as (reused, _):
        assert reused == first
        assert (reused / 'cached-microdesc-consensus').read_text() == 'public directory information'
    assert json.loads(rendezvous.tor_path(first)) == str(first)


def test_tor_cache_lease_survives_parent_context_while_child_is_alive(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    child = None
    try:
        with rendezvous.tor_cache(tmp_path) as (first, lease):
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], pass_fds=(lease,))
        with rendezvous.tor_cache(tmp_path) as (second, _):
            assert second != first
        child.terminate()
        child.wait(timeout=5)
        with rendezvous.tor_cache(tmp_path) as (reused, _):
            assert reused == first
    finally:
        if child and child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize('unsafe', ['symlink', 'permissions', 'lease-symlink'])
def test_tor_cache_rejects_unsafe_paths(tmp_path, monkeypatch, unsafe):
    monkeypatch.setenv('HOME', str(tmp_path))
    base = tmp_path / '.local/share/rendezvous'
    base.mkdir(mode=0o700, parents=True)
    root = tmp_path / 'session'
    root.mkdir()
    if unsafe == 'symlink':
        (base / 'tor-cache').symlink_to(root, target_is_directory=True)
    elif unsafe == 'permissions':
        base.chmod(0o755)
    else:
        slot = base / 'tor-cache/slot-0'
        slot.mkdir(mode=0o700, parents=True)
        (base / 'tor-cache').chmod(0o700)
        secret = root / 'secret'
        secret.write_text('must not touch')
        (slot / '.lease').symlink_to(secret)
    with rendezvous.tor_cache(root) as (cache, lease):
        assert cache == root / 'cache'
        assert lease is None
    if unsafe == 'lease-symlink':
        assert secret.read_text() == 'must not touch'


# Run the actual join path in a subprocess: stream closure must not damage pytest.
# Only Tor discovery/transport is replaced; authentication and PTY SSH are real.
JOIN_PROBE = r'''
import asyncio, contextlib, fcntl, json, os, signal, socket, sys, termios
from types import SimpleNamespace
import rendezvous
@contextlib.asynccontextmanager
async def tor(root, extra):
    yield
async def port(root):
    return int(os.environ['TEST_PORT'])
async def connect(port, hostname):
    sock = socket.socket()
    sock.setblocking(False)
    await asyncio.get_running_loop().sock_connect(sock, ('127.0.0.1', port))
    return sock
rendezvous.tor = tor
rendezvous.tor_socks_port = port
rendezvous.socks_connect = connect
flags = [fcntl.fcntl(fd, fcntl.F_GETFL) for fd in range(3)]
settings = termios.tcgetattr(0) if os.isatty(0) else None
async def run():
    task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        return await rendezvous.join(SimpleNamespace(invitation_file=None,
            invitation=os.environ['TEST_INVITATION'], command=os.environ.get('TEST_COMMAND')))
    except (asyncio.CancelledError, rendezvous.asyncssh.Error):
        return 1
result = asyncio.run(run())
assert all(not stream.closed and not stream.buffer.closed for stream in [sys.stdin, sys.stdout, sys.stderr])
assert flags == [fcntl.fcntl(fd, fcntl.F_GETFL) for fd in range(3)]
assert settings == (termios.tcgetattr(0) if os.isatty(0) else None)
print('STREAMS-RESTORED', flush=True)
print('STDERR-RESTORED', file=sys.stderr, flush=True)
sys.exit(result)
'''


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['exit', 'terminate', 'disconnect'])
async def test_join_interactive_pty_restores_terminal_and_streams(ending):
    gate = Gate(secrets.token_urlsafe(32), time.monotonic() + 30)
    key = asyncssh.generate_private_key('ssh-ed25519')
    server = await asyncssh.create_server(lambda: Server(gate), '127.0.0.1', 0,
        server_host_keys=[key], process_factory=lambda p: shell_session(p, gate), encoding=None)
    invitation = encode_invitation(dict(onion='a' * 56 + '.onion', auth='A' * 52,
        secret=gate.secret, fingerprint=key.get_fingerprint(), expires=int(time.time()) + 30))
    master, slave = pty.openpty()
    settings = termios.tcgetattr(slave)
    flags = fcntl.fcntl(slave, fcntl.F_GETFL)
    os.set_blocking(master, False)
    child = None
    output = bytearray()
    async def collect():
        while True:
            try:
                output.extend(os.read(master, 65536))
            except BlockingIOError:
                pass
            await asyncio.sleep(0.01)
    reader = asyncio.create_task(collect())
    try:
        child = await asyncio.create_subprocess_exec(sys.executable, '-c', JOIN_PROBE,
            stdin=slave, stdout=slave, stderr=slave, cwd=str(Path(__file__).resolve().parents[1]),
            env=dict(os.environ, TEST_PORT=str(server.get_port()), TEST_INVITATION=invitation, NO_COLOR=''))
        async def ready():
            while termios.tcgetattr(slave) == settings:
                assert child.returncode is None, bytes(output)
                await asyncio.sleep(0.01)
        await asyncio.wait_for(ready(), 5)
        if ending == 'exit':
            os.write(master, b"printf 'REMOTE-%s\\n' OK; exit 23\n")
        else:
            # Wait for a real shell/output before interrupting or losing transport.
            os.write(master, b"printf 'REMOTE-%s\\n' OK\n")
        async def remote_ready():
            while b'REMOTE-OK' not in output:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(remote_ready(), 5)
        if ending == 'terminate':
            child.send_signal(signal.SIGTERM)
        elif ending == 'disconnect':
            for conn in list(gate.connections):
                conn.abort()
        await asyncio.wait_for(child.wait(), 5)
        await asyncio.sleep(0.05)
        assert b'STREAMS-RESTORED' in output, bytes(output)
        assert b'STDERR-RESTORED' in output, bytes(output)
        assert b'Traceback' not in output and b'lost sys.stderr' not in output, bytes(output)
        assert child.returncode == (23 if ending == 'exit' else 1), bytes(output)
        assert termios.tcgetattr(slave) == settings
        assert fcntl.fcntl(slave, fcntl.F_GETFL) == flags
    finally:
        if child and child.returncode is None:
            child.kill()
            await child.wait()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        os.close(master)
        os.close(slave)
        for conn in list(gate.connections):
            conn.abort()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_join_redirected_streams_and_remote_exit_status(tmp_path):
    gate = Gate(secrets.token_urlsafe(32), time.monotonic() + 30)
    key = asyncssh.generate_private_key('ssh-ed25519')
    server = await asyncssh.create_server(lambda: Server(gate), '127.0.0.1', 0,
        server_host_keys=[key], process_factory=lambda p: shell_session(p, gate), encoding=None)
    invitation = encode_invitation(dict(onion='a' * 56 + '.onion', auth='A' * 52,
        secret=gate.secret, fingerprint=key.get_fingerprint(), expires=int(time.time()) + 30))
    child = None
    try:
        with (tmp_path / 'out').open('wb') as output:
            child = await asyncio.create_subprocess_exec(sys.executable, '-c', JOIN_PROBE,
                stdin=asyncio.subprocess.DEVNULL, stdout=output, stderr=asyncio.subprocess.PIPE,
                cwd=str(Path(__file__).resolve().parents[1]), env=dict(os.environ, TEST_PORT=str(server.get_port()),
                    TEST_INVITATION=invitation, TEST_COMMAND="printf 'COMMAND-OK'; exit 17"))
            _, errors = await asyncio.wait_for(child.communicate(), 10)
        assert child.returncode == 17, errors
        assert b'COMMAND-OK' in (tmp_path / 'out').read_bytes()
        assert b'STREAMS-RESTORED' in (tmp_path / 'out').read_bytes()
        assert b'STDERR-RESTORED' in errors
    finally:
        if child and child.returncode is None:
            child.kill()
            await child.wait()
        for conn in list(gate.connections):
            conn.abort()
        server.close()
        await server.wait_closed()
@pytest.mark.asyncio
async def test_real_ssh_single_use_race_and_replay():
    secret = secrets.token_urlsafe(32)
    gate = Gate(secret, time.monotonic() + 30)
    key = asyncssh.generate_private_key('ssh-ed25519')
    server = await asyncssh.create_server(lambda: Server(gate), '127.0.0.1', 0,
        server_host_keys=[key], process_factory=lambda p: shell_session(p, gate), encoding=None)

    async def connect(password=secret, fingerprint=None):
        return await asyncssh.connect('127.0.0.1', server.get_port(), username='rendezvous', password=password,
            client_keys=[], agent_path=None, known_hosts=(),
            client_factory=lambda: PinnedClient(fingerprint or key.get_fingerprint()))

    winner = None
    try:
        with pytest.raises(asyncssh.HostKeyNotVerifiable):
            await connect(fingerprint=asyncssh.generate_private_key('ssh-ed25519').get_fingerprint())
        assert gate.owner is None
        with pytest.raises(asyncssh.PermissionDenied):
            await connect(password='wrong')
        assert gate.owner is None
        results = await asyncio.gather(*(connect() for _ in range(8)), return_exceptions=True)
        winners = [result for result in results if not isinstance(result, Exception)]
        assert len(winners) == 1
        assert sum(isinstance(result, asyncssh.PermissionDenied) for result in results) == 7
        winner = winners[0]
        with pytest.raises(asyncssh.ChannelOpenError):
            await winner.open_connection('127.0.0.1', 80)
        result = await asyncio.wait_for(winner.run('printf rendezvous-ok', term_type='xterm'), 10)
        assert result.stdout == 'rendezvous-ok'
        assert result.exit_status == 0
        result = await winner.run('printf should-not-run')
        assert result.exit_status == 1
        assert not result.stdout
        winner.close()
        await winner.wait_closed()
        with pytest.raises(asyncssh.PermissionDenied):
            await connect()
    finally:
        if winner:
            winner.close()
            await winner.wait_closed()
        server.close()
        await server.wait_closed()


def test_terminal_colors_leave_piped_invitations_plain(monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setenv('TERM', 'xterm-256color')
    assert rendezvous.paint('rv1.test', '1;36', Terminal()) == '\033[1;36mrv1.test\033[0m'
    assert rendezvous.paint('rv1.test', '1;36', io.StringIO()) == 'rv1.test'
    monkeypatch.setenv('NO_COLOR', '')
    assert rendezvous.paint('rv1.test', '1;36', Terminal()) == 'rv1.test'


def test_devnull_stdin_maps_to_ssh_eof_but_files_remain_readable(tmp_path):
    with open(os.devnull, 'rb') as stream:
        assert rendezvous.ssh_stdin(stream) == asyncssh.DEVNULL
    file = tmp_path / 'input'
    file.write_bytes(b'input data')
    with file.open('rb') as stream:
        assert rendezvous.ssh_stdin(stream) is stream


@pytest.mark.asyncio
@pytest.mark.parametrize('single_hop', [False, True])
async def test_host_reports_connection_immediately_and_enforces_session_deadline(monkeypatch, capsys, single_hop):
    accepted = []
    listening = asyncio.Event()
    connected = asyncio.Event()
    state = {}
    create_server = asyncssh.create_server

    async def recording_server(factory, *args, **kwargs):
        def make_server():
            instance = factory()
            accepted.append(instance)
            return instance
        actual = await create_server(make_server, *args, **kwargs)
        state['port'] = actual.get_port()
        listening.set()
        class Acceptor:
            def close(self):
                actual.close()
            def get_port(self):
                return actual.get_port()
            async def wait_closed(self):
                # Model the newer asyncio behavior which exposed the production bug.
                await asyncio.gather(*(s.conn.wait_closed() for s in accepted))
                await actual.wait_closed()
        return Acceptor()

    @asynccontextmanager
    async def fake_tor(root, extra):
        assert ('HiddenServiceSingleHopMode 1' in extra) is single_hop
        assert ('HiddenServiceNonAnonymousMode 1' in extra) is single_hop
        assert 'SocksPort 0\n' in extra
        (root / 'service/hostname').write_text('a' * 56 + '.onion')
        class Process:
            async def wait(self):
                await asyncio.Event().wait()
        yield Process()

    messages = []
    def record_status(message, code='90'):
        messages.append(message)
        if message == 'Connected.':
            connected.set()
    monkeypatch.setattr(asyncssh, 'create_server', recording_server)
    monkeypatch.setattr(rendezvous, 'tor', fake_tor)
    monkeypatch.setattr(rendezvous, 'status', record_status)
    task = asyncio.create_task(rendezvous.host(SimpleNamespace(invite_timeout=5, session_timeout=0.5, single_hop=single_hop)))
    conn = None
    try:
        await asyncio.wait_for(listening.wait(), 2)
        await asyncio.sleep(0)
        invitation = decode_invitation(capsys.readouterr().out.strip())
        conn = await asyncssh.connect('127.0.0.1', state['port'], username='rendezvous', password=invitation['secret'],
            config=None, client_keys=[], agent_path=None, known_hosts=(), client_factory=lambda: PinnedClient(invitation['fingerprint']))
        await asyncio.wait_for(connected.wait(), 0.25)
        assert not conn.is_closed()
        assert 'Waiting for single-use connection.' in messages
        await asyncio.wait_for(task, 2)
        await asyncio.wait_for(conn.wait_closed(), 1)
        assert messages[-1] == 'Session ended.'
    finally:
        if conn:
            conn.abort()
            await conn.wait_closed()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_expired_gate():
    gate = Gate('secret', time.monotonic() - 1)
    assert not gate.claim(object(), 'rendezvous', 'secret')


def test_invitation_validation():
    data = dict(onion='a' * 56 + '.onion', auth='A' * 52, secret='a' * 43,
                fingerprint='SHA256:' + 'a' * 43, expires=int(time.time()) + 30)
    assert decode_invitation(encode_invitation(data)) == data
    for field, value in [('onion', '127.0.0.1'), ('auth', '../evil\n'), ('expires', 0)]:
        with pytest.raises(ValueError):
            decode_invitation(encode_invitation(dict(data, **{field: value})))


@pytest.mark.asyncio
async def test_disconnect_cleans_up_shell():
    gate = Gate('secret', time.monotonic() + 30)
    key = asyncssh.generate_private_key('ssh-ed25519')
    cleaned = asyncio.Event()
    async def session(process):
        try:
            await shell_session(process, gate)
        finally:
            cleaned.set()
    server = await asyncssh.create_server(lambda: Server(gate), '127.0.0.1', 0,
        server_host_keys=[key], process_factory=session, encoding=None)
    conn = None
    try:
        conn = await asyncssh.connect('127.0.0.1', server.get_port(), username='rendezvous', password='secret',
            config=None, client_keys=[], agent_path=None, known_hosts=(), client_factory=lambda: PinnedClient(key.get_fingerprint()))
        process = await conn.create_process('printf "%s\\n" "$$"; sleep 60', term_type='xterm')
        pid = int((await asyncio.wait_for(process.stdout.readline(), 5)).strip())
        os.kill(pid, 0)
        conn.abort()
        await asyncio.wait_for(cleaned.wait(), 5)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if conn:
            conn.abort()
            await conn.wait_closed()
        server.close()
        await server.wait_closed()
