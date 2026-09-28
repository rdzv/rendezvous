import asyncio
from contextlib import asynccontextmanager
import io
import os
import time
from types import SimpleNamespace

import asyncssh
import pyte
import pytest
import rendezvous


@pytest.mark.asyncio
async def test_publication_subscribes_before_network_and_requires_acks(tmp_path):
    (tmp_path / 'data').mkdir()
    (tmp_path / 'service').mkdir()
    (tmp_path / 'service/hostname').write_text('a' * 56 + '.onion')
    (tmp_path / 'data/control_auth_cookie').write_bytes(b'x' * 32)
    commands = []
    seventh = asyncio.Event()
    async def controller(reader, writer):
        try:
            for _ in range(3):
                commands.append((await reader.readline()).strip())
                writer.write(b'250 OK\r\n')
                await writer.drain()
            assert commands == [b'AUTHENTICATE ' + (b'x' * 32).hex().encode(), b'SETEVENTS HS_DESC', b'SETCONF DisableNetwork=0']
            # Send 7 uploads (including duplicates and wrong onion), then wait for the 8th
            # v3 onion services require hsdir_n_replicas (2) × hsdir_spread_store (4) = 8 unique uploads
            for onion, hsdir in [
                ('b' * 56, '$wrong'),      # wrong onion, ignored
                ('a' * 56, '$one'),         # 1st unique
                ('a' * 56, '$one'),         # duplicate, ignored
                ('a' * 56, '$two'),         # 2nd unique
                ('a' * 56, '$three'),       # 3rd unique
                ('a' * 56, '$four'),        # 4th unique
                ('a' * 56, '$five'),        # 5th unique
                ('a' * 56, '$six'),         # 6th unique
                ('a' * 56, '$seven'),       # 7th unique
            ]:
                writer.write(f'650 HS_DESC UPLOADED {onion} NO_AUTH {hsdir}\r\n'.encode())
            await writer.drain()
            await seventh.wait()
            writer.write(b'650 HS_DESC UPLOADED ' + b'a' * 56 + b' NO_AUTH $eight\r\n')
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_unix_server(controller, path=str(tmp_path / 'control'))
    publication = await rendezvous.Publication.connect(tmp_path, SimpleNamespace(returncode=None))
    task = asyncio.create_task(publication.wait())
    try:
        await asyncio.sleep(0.05)
        assert not task.done()
        seventh.set()
        await asyncio.wait_for(task, 1)
    finally:
        publication.writer.close()
        await publication.writer.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['socks', 'handshake', 'after-auth', 'pin'])
async def test_retries_stop_before_ambiguous_authentication(monkeypatch, failure):
    @asynccontextmanager
    async def tor(root, extra):
        yield SimpleNamespace(returncode=None)
    async def port(root):
        return 1234
    calls = []
    sockets = []
    class Socket:
        closed = False
        def close(self):
            self.closed = True
    class Connection:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    async def socks(port, onion):
        calls.append('socks')
        if failure == 'socks' and calls.count('socks') == 1:
            raise OSError('unreachable')
        sock = Socket()
        sockets.append(sock)
        return sock
    async def connect(*args, **kwargs):
        calls.append('ssh')
        if failure == 'pin':
            raise asyncssh.HostKeyNotVerifiable('wrong pin')
        if failure == 'after-auth':
            assert kwargs['password']() == 'secret'
            raise asyncssh.ConnectionLost('lost')
        if failure == 'handshake' and calls.count('ssh') == 1:
            raise asyncssh.ConnectionLost('lost')
        kwargs['password']()
        return Connection()
    sleep = asyncio.sleep
    async def quick_sleep(delay):
        await sleep(0)
    monkeypatch.setattr(rendezvous, 'tor', tor)
    monkeypatch.setattr(rendezvous, 'tor_socks_port', port)
    monkeypatch.setattr(rendezvous, 'socks_connect', socks)
    monkeypatch.setattr(asyncssh, 'connect', connect)
    monkeypatch.setattr(asyncio, 'sleep', quick_sleep)
    invitation = dict(onion='a' * 56 + '.onion', auth='A' * 52, secret='secret', fingerprint='pin', expires=int(time.time()) + 30)
    async def run():
        async with rendezvous.joined_connection(invitation):
            pass
    if failure == 'after-auth':
        with pytest.raises(RuntimeError, match='may have been consumed'):
            await run()
        assert calls.count('ssh') == 1
    elif failure == 'pin':
        with pytest.raises(asyncssh.HostKeyNotVerifiable):
            await run()
        assert calls.count('ssh') == 1
    else:
        await run()
        assert calls.count('socks') == 2
    assert all(sock.closed for sock in sockets)


def test_log_colors_alignment_and_split_character_sets(monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    output = Terminal()
    monkeypatch.setattr(rendezvous.sys, 'stderr', output)
    monkeypatch.setenv('TERM', 'xterm')
    monkeypatch.delenv('NO_COLOR', raising=False)
    log = rendezvous.HostLog()
    log.input(b'whoami\r')
    for char in b'\x1b(B\x1b)0radioadmin B\x1b(B\r\n':
        log.output(bytes([char]))
    screen = pyte.Screen(80, 24)
    pyte.Stream(screen).feed(output.getvalue().replace('\n', '\r\n'))
    assert screen.display[0].rstrip() == '[input]   whoami'
    assert screen.display[1].rstrip() == '[output]  radioadmin B'
    assert screen.buffer[0][0].fg == screen.buffer[1][0].fg == 'default'
    assert screen.buffer[0][10].fg == 'red'
    assert screen.buffer[1][10].fg == 'cyan'
