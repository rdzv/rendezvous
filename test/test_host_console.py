import asyncio
from contextlib import asynccontextmanager
import fcntl
import io
import json
import os
from pathlib import Path
import pty
import signal
import sys
import termios
import struct
from types import SimpleNamespace

import asyncssh
import pytest
import pyte

import rendezvous


def test_host_transcript_keeps_complete_input_and_escapes_terminal_controls(capsys):
    log = rendezvous.HostLog()
    command = 'printf "hello"\n' + 'x' * 12000 + '\x1b[2J\u202e'
    log.command(command)
    assert json.loads('"' + capsys.readouterr().err.strip()[len('[command] '):] + '"') == command
    incoming = b'long-command ' + b'x' * 12000 + b'\x7f\x1b[A\r\nunfinished'
    for offset in range(0, len(incoming), 37):
        log.input(incoming[offset:offset + 37])
    log.flush()
    records = capsys.readouterr().err.splitlines()
    assert len(records) == 2
    assert '\n'.join(json.loads('"' + line[10:] + '"') for line in records).encode() == incoming.replace(b'\r\n', b'\n')
    assert all('\x1b' not in line and '\r' not in line and '\n' not in line for line in records)


def test_host_output_preview_and_verbose_preserve_guest_data(monkeypatch, capsys):
    monkeypatch.setattr(rendezvous.shutil, 'get_terminal_size', lambda: os.terminal_size((80, 24)))
    data = b'first ' + b'x' * 1000 + b'\nsecond\x1b[2J\n'
    rendezvous.HostLog().output(data)
    records = capsys.readouterr().err.splitlines()
    assert len(records) == 2 and len(records[0]) <= 80
    assert records[0].endswith(' ...') and 'second' not in records[0]
    assert records[1] == '[output]  second'
    rendezvous.HostLog(verbose=True).output(data)
    records = capsys.readouterr().err.splitlines()
    assert len(records) == 2
    assert '\n'.join(json.loads('"' + line[10:] + '"') for line in records).encode() + b'\n' == data
    assert all('\x1b' not in line for line in records)


@pytest.mark.parametrize('verbose', [False, True])
def test_fragmented_input_output_follow_real_line_boundaries(capsys, verbose):
    log = rendezvous.HostLog(verbose)
    for char in b'pwd':
        log.input(bytes([char]))
    assert capsys.readouterr().err == ('' if verbose else '[input]   pwd')
    log.input(b'\r')
    log.input(b'\n')
    assert capsys.readouterr().err == ('[input]   pwd\n' if verbose else '\n')
    for char in b'/home/radioadmin':
        log.output(bytes([char]))
    assert capsys.readouterr().err == ''
    log.output(b'\r')
    log.output(b'\n')
    assert capsys.readouterr().err == '[output]  /home/radioadmin\n'
    log.output(b'partial')
    log.flush()
    assert capsys.readouterr().err == '[output]  partial\n'


@pytest.mark.parametrize('chunk_size', [1, 7, 65536])
def test_bash_prompts_and_typing_echo_hidden_except_in_verbose(capsys, chunk_size):
    prompt = b'\x1b[?2004h\x1b]0;radioadmin@host: ~\x07\x1b[32mradioadmin@host\x1b[0m:~$ '
    for verbose in [False, True]:
        log = rendezvous.HostLog(verbose)
        for offset in range(0, len(prompt), chunk_size):
            log.output(prompt[offset:offset + chunk_size])
        for char in b'pwd':
            log.input(bytes([char]))
            log.output(bytes([char]))
        log.input(b'\r')
        data = b'\r\n\x1b[?2004l\r/home/radioadmin\r\n' + prompt
        for offset in range(0, len(data), chunk_size):
            log.output(data[offset:offset + chunk_size])
        log.flush()
        text = capsys.readouterr().err
        if verbose:
            assert 'radioadmin@host' in text and '?2004h' in text
        else:
            assert text == '[input]   pwd\n[output]  /home/radioadmin\n'
        assert '\x1b' not in text


def test_plain_prompt_hidden_and_long_output_bounded(capsys):
    log = rendezvous.HostLog()
    log.output(b'$ ')
    log.input(b'ls\r')
    log.output(b'ls\r\nfile.txt\r\n$ ')
    log.flush()
    assert capsys.readouterr().err == '[input]   ls\n[output]  file.txt\n'
    log.output(b'x' * 100000)
    assert len(log.out) < 1000
    log.output(b'\n')
    assert capsys.readouterr().err.endswith(' ...\n')
    log.output(b'$ \r\n')
    assert capsys.readouterr().err == '[output]  $ \n'  # Newline-terminated program output.


def test_persistent_status_bar_scroll_resize_and_cleanup(monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True
        def fileno(self):
            return 2
    terminal = Terminal()
    monkeypatch.setattr(sys, 'stderr', terminal)
    monkeypatch.setenv('TERM', 'xterm-256color')
    monkeypatch.setenv('NO_COLOR', '')
    size = [80, 24]
    monkeypatch.setattr(os, 'get_terminal_size', lambda fd: os.terminal_size(size))
    bar = rendezvous.HostStatusBar()
    bar.resize()
    for index in range(100):
        rendezvous.status(f'line {index}')
    screen = pyte.Screen(*size)
    stream = pyte.Stream(screen)
    stream.feed(terminal.getvalue().replace('\n', '\r\n'))
    assert screen.display[-1].strip() == 'D: disconnect'
    assert screen.display[-3].strip() == 'line 99'
    assert 'Ctrl-C' not in terminal.getvalue()
    assert screen.margins.bottom == 22
    terminal.seek(0); terminal.truncate()
    rendezvous.HostLog().input(b'\x1b[r' + b'x' * 2000 + b'\r')
    stream.feed(terminal.getvalue().replace('\n', '\r\n'))
    assert screen.display[-1].strip() == 'D: disconnect'
    assert screen.margins.bottom == 22
    terminal.seek(0); terminal.truncate()
    size[:] = [60, 16]
    screen.resize(lines=16, columns=60)
    bar.resize()
    for index in range(30):
        rendezvous.status(f'resized {index}')
    stream.feed(terminal.getvalue().replace('\n', '\r\n'))
    assert screen.display[-1].strip() == 'D: disconnect'
    assert screen.display[-3].strip() == 'resized 29'
    terminal.seek(0); terminal.truncate()
    bar.close()
    stream.feed(terminal.getvalue())
    assert screen.display[-1].strip() == ''
    assert screen.margins is None


@pytest.mark.asyncio
async def test_tor_transport_has_its_own_session_and_is_still_cleaned_up(tmp_path, monkeypatch):
    executable = tmp_path / 'fake-tor'
    executable.write_text(f'#!{sys.executable}\nimport time\nprint("Bootstrapped 100%", flush=True)\ntime.sleep(60)\n')
    executable.chmod(0o700)
    monkeypatch.setattr(rendezvous.shutil, 'which', lambda name: str(executable))
    async with rendezvous.tor_process(tmp_path, '', tmp_path / 'cache', None) as process:
        assert os.getsid(process.pid) == process.pid
        assert os.getpgid(process.pid) != os.getpgrp()
    assert process.returncode == -signal.SIGTERM


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['cancel', 'deadline'])
async def test_host_notifies_client_before_stopping_tor(monkeypatch, capsys, ending):
    listening = asyncio.Event()
    received = asyncio.Event()
    stopped = asyncio.Event()
    state = {}
    create_server = asyncssh.create_server
    async def record_server(*args, **kwargs):
        server = await create_server(*args, **kwargs)
        state['port'] = server.get_port()
        listening.set()
        return server
    @asynccontextmanager
    async def fake_tor(root, extra):
        (root / 'service/hostname').write_text('a' * 56 + '.onion')
        class Process:
            async def wait(self):
                await asyncio.Event().wait()
        try:
            yield Process()
        finally:
            assert received.is_set(), 'Tor stopped before the client received graceful shutdown'
            stopped.set()
    monkeypatch.setattr(asyncssh, 'create_server', record_server)
    monkeypatch.setattr(rendezvous, 'tor', fake_tor)
    host = asyncio.create_task(rendezvous.host(SimpleNamespace(invite_timeout=5,
        session_timeout=0.75 if ending == 'deadline' else 30, single_hop=False, verbose=False)))
    conn = None
    try:
        await asyncio.wait_for(listening.wait(), 2)
        await asyncio.sleep(0)
        invitation = rendezvous.decode_invitation(capsys.readouterr().out.strip())
        conn = await asyncssh.connect('127.0.0.1', state['port'], username='rendezvous',
            password=invitation['secret'], config=None, client_keys=[], agent_path=None,
            known_hosts=(), client_factory=lambda: rendezvous.PinnedClient(invitation['fingerprint']))
        process = await conn.create_process('printf ready; sleep 60', term_type='xterm')
        assert await asyncio.wait_for(process.stdout.readexactly(5), 3) == 'ready'
        if ending == 'cancel':
            host.cancel()
        result = await asyncio.wait_for(process.wait(), 3)
        assert 'Session ended by host.' in result.stderr
        assert result.exit_status == 130
        assert not stopped.is_set()
        received.set()
        conn.close()
        await conn.wait_closed()
        results = await asyncio.wait_for(asyncio.gather(host, return_exceptions=True), 3)
        assert results == [None] if ending == 'deadline' else isinstance(results[0], asyncio.CancelledError)
        assert stopped.is_set()
        transcript = capsys.readouterr().err
        assert '[command] printf ready; sleep 60' in transcript
        assert '[output]  ready' in transcript
    finally:
        if conn:
            conn.abort()
            await conn.wait_closed()
        if not host.done():
            host.cancel()
        await asyncio.gather(host, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('key', [b'D', b'\x03'])
@pytest.mark.parametrize('tty_logs', [False, True])
async def test_local_host_controls_work_with_piped_stdin_and_restore_terminal(key, tty_logs):
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (24, 80))
    os.set_blocking(master, False)
    original = termios.tcgetattr(slave)
    terminal_output = bytearray()
    async def collect():
        while True:
            try:
                terminal_output.extend(os.read(master, 65536))
            except BlockingIOError:
                pass
            await asyncio.sleep(0.005)
    reader = asyncio.create_task(collect())
    script = f'''
import os,fcntl,termios,asyncio
os.setsid()
fcntl.ioctl({slave},termios.TIOCSCTTY,0)
import rendezvous
async def run():
    try:
        with rendezvous.host_controls():
            print('READY', flush=True)
            await asyncio.Event().wait()
    except asyncio.CancelledError:
        print('STOPPED', flush=True)
asyncio.run(run())
'''
    child = None
    try:
        child = await asyncio.create_subprocess_exec(sys.executable, '-c', script,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=slave if tty_logs else asyncio.subprocess.PIPE,
            pass_fds=(slave,), cwd=str(Path(__file__).resolve().parents[1]), env=dict(os.environ, TERM='xterm-256color', NO_COLOR='1'))
        assert await asyncio.wait_for(child.stdout.readline(), 3) == b'READY\n'
        # A fragmented left-arrow escape sequence ends in D, but is not the D key.
        os.write(master, b'\x1b')
        await asyncio.sleep(0.02)
        os.write(master, b'[D')
        await asyncio.sleep(0.02)
        assert child.returncode is None
        os.write(master, b'd')  # Lowercase d is no longer a disconnect binding.
        await asyncio.sleep(0.02)
        assert child.returncode is None
        os.write(master, b'\x1b')
        await asyncio.sleep(0.3)  # A stale standalone Escape must not swallow D.
        if tty_logs:
            termios.tcsetwinsize(slave, (18, 60))
            child.send_signal(signal.SIGWINCH)
            await asyncio.sleep(0.03)
        os.write(master, key)
        stdout, stderr = await asyncio.wait_for(child.communicate(), 3)
        await asyncio.sleep(0.02)
        stderr = bytes(terminal_output) if tty_logs else stderr
        assert child.returncode == 0, stderr
        assert b'STOPPED' in stdout
        assert b'D or Ctrl-C: disconnect.' not in stderr
        if tty_logs:
            assert b'D: disconnect' in stderr
            assert b'\x1b[1;23r' in stderr and b'\x1b[1;17r' in stderr
            assert stderr.endswith(b'\x1b[r\x1b[18;1H\x1b[2K')
        else:
            assert b'\x1b' not in stderr  # No status-bar escape codes in redirected logs.
        assert termios.tcgetattr(slave) == original
    finally:
        if child and child.returncode is None:
            child.kill()
            await child.wait()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        os.close(master)
        os.close(slave)
