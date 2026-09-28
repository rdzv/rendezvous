import asyncio
import io
import os
from pathlib import Path
import pty
import sys
import termios

import pyte
import pytest
import rendezvous


def test_status_bar_exact_text_key_colors_and_pending_visibility(monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True
        def fileno(self):
            return 2
    terminal = Terminal()
    monkeypatch.setattr(sys, 'stderr', terminal)
    monkeypatch.setenv('TERM', 'xterm')
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setattr(os, 'get_terminal_size', lambda fd: os.terminal_size((80, 24)))
    bar = rendezvous.HostStatusBar()
    screen = pyte.Screen(80, 24)
    stream = pyte.Stream(screen)
    def render():
        stream.feed(terminal.getvalue().replace('\n', '\r\n'))
        terminal.seek(0)
        terminal.truncate()
    bar.resize()
    render()
    assert screen.display[-1].strip() == 'D: disconnect'
    assert screen.buffer[23][0].fg == 'red'
    bar.pending = True
    bar.redraw()
    render()
    expected = 'a: approve, A: approve all, d: deny, D: disconnect'
    assert screen.display[-1].rstrip() == expected
    colors = {expected.index(key + ':'): color for key, color in [('a', 'green'), ('A', 'green'), ('d', 'red'), ('D', 'red')]}
    for index in range(len(expected)):
        assert screen.buffer[23][index].fg == colors.get(index, 'default')
    bar.pending = False
    bar.redraw()
    render()
    assert screen.display[-1].strip() == 'D: disconnect'
    bar.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('key,decision,enabled', [(b'a', True, True), (b'd', False, True), (b'A', True, False)])
async def test_real_terminal_approval_keys_prompts_and_footer(key, decision, enabled):
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (24, 80))
    original = termios.tcgetattr(slave)
    os.set_blocking(master, False)
    output = bytearray()
    async def collect():
        while True:
            try:
                output.extend(os.read(master, 65536))
            except BlockingIOError:
                pass
            await asyncio.sleep(0.005)
    reader = asyncio.create_task(collect())
    script = f'''
import os,fcntl,termios,asyncio
os.setsid()
fcntl.ioctl({slave},termios.TIOCSCTTY,0)
import rendezvous
class Process:
    async def wait_closed(self):
        await asyncio.Event().wait()
    def is_closing(self):
        return False
async def run():
    approval = rendezvous.CommandApproval(True)
    try:
        with rendezvous.host_controls(approval):
            rendezvous.HostLog().emit('input', b'ls /tmp')
            print('READY', flush=True)
            result = await approval.request(Process())
            print(f'DECISION:{{result}}:{{approval.enabled}}', flush=True)
            await asyncio.Event().wait()
    except asyncio.CancelledError:
        print('STOPPED', flush=True)
asyncio.run(run())
'''
    child = None
    try:
        child = await asyncio.create_subprocess_exec(sys.executable, '-c', script, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=slave, pass_fds=(slave,), cwd=str(Path(__file__).resolve().parents[1]),
            env=dict(os.environ, TERM='xterm', NO_COLOR='1'))
        assert await asyncio.wait_for(child.stdout.readline(), 3) == b'READY\n'
        await asyncio.sleep(0.03)
        screen = pyte.Screen(80, 24)
        pyte.Stream(screen).feed(output.decode())
        assert screen.display[-1].rstrip() == 'a: approve, A: approve all, d: deny, D: disconnect'
        assert b'[input]   ls /tmp\r\n' in output
        os.write(master, b'xy')
        await asyncio.sleep(0.03)
        assert b'Approve command? \r\nApprove command (a/A/d)? ' in output
        os.write(master, key)
        assert await asyncio.wait_for(child.stdout.readline(), 3) == f'DECISION:{decision}:{enabled}\n'.encode()
        await asyncio.sleep(0.03)
        screen = pyte.Screen(80, 24)
        pyte.Stream(screen).feed(output.decode())
        assert screen.display[-1].strip() == 'D: disconnect'
        assert b'Approve command (a/A/d)? ' + key + b'\r\n' in output
        os.write(master, b'd')
        await asyncio.sleep(0.03)
        assert child.returncode is None
        os.write(master, b'D')
        await asyncio.wait_for(child.wait(), 3)
        assert child.returncode == 0
        assert termios.tcgetattr(slave) == original
    finally:
        if child and child.returncode is None:
            child.kill()
            await child.wait()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        os.close(master)
        os.close(slave)


def test_headless_approval_fails_closed(monkeypatch):
    def no_terminal(*args, **kwargs):
        raise OSError('no tty')
    monkeypatch.setattr(os, 'open', no_terminal)
    with pytest.raises(RuntimeError, match='controlling terminal'):
        with rendezvous.host_controls(rendezvous.CommandApproval(True)):
            pytest.fail('approval must not silently become allow-all')
