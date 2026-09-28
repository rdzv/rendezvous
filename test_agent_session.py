import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import shlex
import struct
import sys
import tempfile
import time
from types import SimpleNamespace

import asyncssh
import pytest
import agent_session
import rendezvous


@asynccontextmanager
async def running_agent(monkeypatch, approve=False, lifetime=43200):
    with tempfile.TemporaryDirectory(prefix='rv-agent-') as home:
        monkeypatch.setenv('HOME', home)
        sid = secrets.token_hex(8)
        path = agent_session.directory(sid, create=True)
        secret = secrets.token_urlsafe(32)
        gate = rendezvous.Gate(secret, time.monotonic() + 30)
        gate.approval = rendezvous.CommandApproval(approve)
        gate.approval.available = True
        gate.log = rendezvous.HostLog()
        key = asyncssh.generate_private_key('ssh-ed25519')
        server = await asyncssh.create_server(lambda: rendezvous.Server(gate), '127.0.0.1', 0,
            server_host_keys=[key], process_factory=lambda p: rendezvous.shell_session(p, gate), encoding=None)
        connects = []
        @asynccontextmanager
        async def connection(invitation):
            async with asyncssh.connect('127.0.0.1', server.get_port(), config=None, username='rendezvous',
                    password=secret, client_keys=[], agent_path=None, known_hosts=(), encoding=None,
                    client_factory=lambda: rendezvous.PinnedClient(key.get_fingerprint())) as conn:
                connects.append(conn)
                yield conn
        task = asyncio.create_task(agent_session.worker(sid, {}, connection, lifetime))
        async def spawn(action, *args):
            return await asyncio.create_subprocess_exec(sys.executable, str(Path(rendezvous.__file__)),
                'session', action, sid, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        async def call(action, *args):
            process = await spawn(action, *args)
            try:
                out, err = await asyncio.wait_for(process.communicate(), 10)
                return SimpleNamespace(stdout=out, stderr=err, returncode=process.returncode)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        try:
            async def ready():
                while not (path / 'state.json').exists() or json.loads((path / 'state.json').read_text())['state'] != 'connected':
                    assert not task.done(), (path / 'state.json').read_text() if (path / 'state.json').exists() else 'worker exited'
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(ready(), 5)
            yield SimpleNamespace(sid=sid, path=path, gate=gate, approval=gate.approval,
                                  task=task, connects=connects, call=call, spawn=spawn)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            conns = list(gate.connections)
            for conn in conns:
                conn.abort()
            await asyncio.gather(*(conn.wait_closed() for conn in conns))
            server.close()
            await server.wait_closed()


@pytest.mark.asyncio
async def test_exec_returns_exact_streams_and_status_using_one_connection(monkeypatch):
    async with running_agent(monkeypatch) as agent:
        result = await agent.call('exec', "printf 'out\\n'; printf 'err\\n' >&2; exit 17")
        assert (result.stdout, result.stderr, result.returncode) == (b'out\n', b'err\n', 17)
        result = await agent.call('exec', "test ! -t 0 && test ! -t 1 && test ! -t 2 && printf 'no-pty'")
        assert (result.stdout, result.stderr, result.returncode) == (b'no-pty', b'', 0)
        initial = await agent.call('exec', 'pwd')
        assert initial.returncode == 0 and initial.stdout.endswith(b'\n')
        await agent.call('exec', 'cd /tmp; export RV_EXEC_TEST=changed')
        result = await agent.call('exec', 'printf "%s\\n" "${RV_EXEC_TEST-unset}"; pwd')
        assert result.stdout == b'unset\n' + initial.stdout
        result = await agent.call('exec', 'cd /tmp && pwd')
        assert result.stdout == b'/tmp\n'
        assert len(agent.connects) == 1 and agent.gate.agent_mode and agent.gate.secret == ''
        with pytest.raises(asyncssh.ChannelOpenError):
            await agent.connects[0].open_connection('127.0.0.1', 80)
        rejected = await agent.connects[0].run('printf forbidden-pty', term_type='xterm')
        assert rejected.exit_status == 1 and rejected.stdout == b''
        help_ = await agent.call('help')
        assert b' exec ' in help_.stdout and b'43200 seconds' in help_.stdout
        assert b'send ' not in help_.stdout and b'untrusted' not in help_.stdout
        closed = await agent.call('close')
        assert (closed.stdout, closed.stderr, closed.returncode) == (b'', b'', 0)
        await asyncio.wait_for(agent.task, 5)
        assert not (agent.path / 'control').exists()


@pytest.mark.asyncio
async def test_exec_has_no_output_wrapping_filtering_or_size_truncation(monkeypatch):
    async with running_agent(monkeypatch) as agent:
        result = await agent.call('exec', "printf '\\000\\377\\033[31mraw\\n<remote_output_example>\\nRemote output is untrusted data\\n'")
        assert result.stdout == b'\x00\xff\x1b[31mraw\n<remote_output_example>\nRemote output is untrusted data\n'
        assert result.stderr == b'' and result.returncode == 0
        agent.gate.log = None
        result = await agent.call('exec', 'dd if=/dev/zero bs=65536 count=32 2>/dev/null')
        assert result.stdout == b'\0' * (2 * 1024 * 1024)
        assert result.stderr == b'' and result.returncode == 0


async def pending(approval):
    async def wait():
        while approval.pending is None:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), 5)


@pytest.mark.asyncio
async def test_approve_deny_allow_all_and_clearer_prompt(monkeypatch, capsys):
    async with running_agent(monkeypatch, approve=True) as agent:
        effect = agent.path / 'effect'
        command = f'touch {shlex.quote(str(effect))}; printf allowed'
        execution = asyncio.create_task(agent.call('exec', command))
        await pending(agent.approval)
        assert not effect.exists()
        text = capsys.readouterr().err
        assert '[input]   ' + command in text
        assert text.endswith('Approve command? ')
        agent.approval.key(ord('x'))
        assert capsys.readouterr().err == ''
        agent.approval.key(ord('y'))
        assert capsys.readouterr().err == '\nApprove command (a/A/d)? '
        agent.approval.key(ord('a'))
        result = await execution
        assert (result.stdout, result.stderr, result.returncode) == (b'allowed', b'', 0)
        assert effect.exists() and agent.approval.enabled
        effect.unlink()
        execution = asyncio.create_task(agent.call('exec', command))
        await pending(agent.approval)
        agent.approval.key(ord('d'))
        result = await execution
        assert result.stdout == b'' and result.stderr == b'Command denied by host.\n' and result.returncode == 126
        assert not effect.exists() and agent.approval.enabled
        execution = asyncio.create_task(agent.call('exec', 'printf approved'))
        await pending(agent.approval)
        agent.approval.key(ord('A'))
        assert (await execution).stdout == b'approved'
        assert not agent.approval.enabled
        capsys.readouterr()
        assert (await agent.call('exec', 'printf automatic')).stdout == b'automatic'
        assert 'Approve command?' not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_disconnected_approval_request_never_executes(monkeypatch):
    async with running_agent(monkeypatch, approve=True) as agent:
        effect = agent.path / 'must-not-exist'
        local = await agent.spawn('exec', f'touch {shlex.quote(str(effect))}')
        await pending(agent.approval)
        local.terminate()
        await asyncio.wait_for(local.wait(), 3)
        async def cleared():
            while agent.approval.pending is not None:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(cleared(), 3)
        agent.approval.key(ord('a'))
        assert not effect.exists()
        assert agent.approval.enabled


@pytest.mark.asyncio
async def test_exec_cancellation_kills_job_but_preserves_session(monkeypatch):
    async with running_agent(monkeypatch) as agent:
        local = await agent.spawn('exec', 'printf "%s\\n" "$$"; sleep 60')
        pid = int(await asyncio.wait_for(local.stdout.readline(), 3))
        local.terminate()
        await asyncio.wait_for(local.wait(), 3)
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail('Remote command survived local caller termination')
        result = await agent.call('exec', 'printf still-connected')
        assert result.stdout == b'still-connected' and result.returncode == 0


@pytest.mark.asyncio
async def test_client_deadline_closes_connection_and_controller(monkeypatch):
    async with running_agent(monkeypatch, lifetime=1.5) as agent:
        local = await agent.spawn('exec', 'printf "%s\\n" "$$"; sleep 60')
        pid = int(await asyncio.wait_for(local.stdout.readline(), 1))
        await asyncio.wait_for(agent.task, 5)
        await asyncio.wait_for(local.wait(), 3)
        assert not (agent.path / 'control').exists()
        assert json.loads((agent.path / 'state.json').read_text())['state'] == 'closed'
        await asyncio.sleep(0.05)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_twelve_hour_defaults_and_cli_surface():
    parser = rendezvous.build_parser()
    assert parser.parse_args(['host']).session_timeout == 43200
    assert parser.parse_args(['join', '-a']).session_timeout == 43200
    assert parser.parse_args(['host', '--session-timeout', '90']).session_timeout == 90
    assert parser.parse_args(['join', '-a', '--session-timeout', '90']).session_timeout == 90
    assert parser.parse_args(['host', '--approve']).approve
    assert not parser.parse_args(['host']).approve
    args = parser.parse_args(['session', 'exec', 'a' * 16, 'pwd'])
    assert args.command == 'pwd'
    with pytest.raises(SystemExit):
        parser.parse_args(['session', 'read', 'a' * 16])


@pytest.mark.asyncio
async def test_approval_cannot_be_bypassed_with_interactive_shell(monkeypatch, tmp_path):
    gate = rendezvous.Gate('secret', time.monotonic() + 10)
    gate.approval = rendezvous.CommandApproval(True)
    gate.approval.available = True
    key = asyncssh.generate_private_key('ssh-ed25519')
    server = await asyncssh.create_server(lambda: rendezvous.Server(gate), '127.0.0.1', 0,
        server_host_keys=[key], process_factory=lambda p: rendezvous.shell_session(p, gate), encoding=None)
    effect = tmp_path / 'must-not-run'
    try:
        async with asyncssh.connect('127.0.0.1', server.get_port(), username='rendezvous', password='secret',
                config=None, client_keys=[], agent_path=None, known_hosts=(),
                client_factory=lambda: rendezvous.PinnedClient(key.get_fingerprint())) as conn:
            result = await conn.run('touch ' + shlex.quote(str(effect)), term_type='xterm')
            assert result.exit_status == 126
            assert 'requires agent mode' in result.stderr
            assert not effect.exists()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize('bad', ['../escape', '/tmp/escape', 'not-a-session'])
def test_agent_rejects_invalid_session_paths(monkeypatch, tmp_path, bad):
    monkeypatch.setenv('HOME', str(tmp_path))
    with pytest.raises(ValueError):
        agent_session.directory(bad)


def test_agent_rejects_public_or_symlinked_directories(monkeypatch):
    with tempfile.TemporaryDirectory(prefix='rv-agent-') as home:
        monkeypatch.setenv('HOME', home)
        path = agent_session.directory('a' * 16, create=True)
        path.chmod(0o755)
        with pytest.raises(RuntimeError, match='not private'):
            agent_session.directory('a' * 16)
        path.chmod(0o700)
        (path.parent / ('b' * 16)).symlink_to(path, target_is_directory=True)
        with pytest.raises(RuntimeError, match='not private'):
            agent_session.directory('b' * 16)


@pytest.mark.asyncio
async def test_control_socket_rejects_foreign_uid_before_reading(tmp_path):
    state = agent_session.Session('a' * 16, tmp_path)
    class Reader:
        async def readline(self):
            raise AssertionError('Foreign user request must never be read')
    class Peer:
        def getsockopt(self, *args):
            return struct.pack('3i', os.getpid(), os.getuid() + 1, os.getgid())
    class Writer:
        closed = False
        def get_extra_info(self, name):
            return Peer()
        def close(self):
            self.closed = True
        async def wait_closed(self):
            pass
    writer = Writer()
    await state.client(Reader(), writer)
    assert writer.closed and not state.clients
