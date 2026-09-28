"""Real Tor: native exec results, host approval, auto-approval, and disconnect."""
import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import pty
import re
import signal
import sys
import termios
import time


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cli', nargs='+', default=[str(Path.home() / '.local/bin/rdzv')])
    args = parser.parse_args()
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (24, 100))
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
    collector = asyncio.create_task(collect())
    async def expect(text, after=0, timeout=30):
        async def wait():
            while text not in output[after:]:
                assert host.returncode is None, output.decode(errors='replace')
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), timeout)
    host = session = None
    active = set()
    async def spawn(*arguments):
        process = await asyncio.create_subprocess_exec(*args.cli, *arguments, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        active.add(process)
        return process
    async def finish(process, timeout=30):
        out, err = await asyncio.wait_for(process.communicate(), timeout)
        active.discard(process)
        return process.returncode, out, err
    async def execute(command, approval=None):
        offset = len(output)
        process = await spawn('session', 'exec', session, command)
        if approval:
            await expect(b'Approve command? ', offset)
            assert b'a: approve, A: approve all, d: deny, D: disconnect' in output[offset:]
            os.write(master, approval)
        return await finish(process)
    try:
        launcher = f'import os,fcntl,termios,sys; os.setsid(); fcntl.ioctl({slave},termios.TIOCSCTTY,0); os.execv(sys.argv[1],sys.argv[1:])'
        bootstrap = os.environ.get('HOST_BOOTSTRAP')
        command = ['/bin/sh', '-s', '--', 'host'] if bootstrap else [*args.cli, 'host']
        started = time.monotonic()
        host = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', launcher,
            *command, '--approve', '--invite-timeout', '300', '--session-timeout', '300',
            stdin=asyncio.subprocess.PIPE if bootstrap else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=slave, pass_fds=(slave,),
            env=dict(os.environ, TERM='xterm-256color', NO_COLOR='1'))
        if bootstrap:
            host.stdin.write(Path(bootstrap).read_bytes())
            await host.stdin.drain()
            host.stdin.close()
        invitation = await asyncio.wait_for(host.stdout.readline(), 380)
        assert invitation.startswith(b'rv1.'), output.decode(errors='replace')
        publication = round(time.monotonic() - started, 2)
        joined = await spawn('join', '-a', '--session-timeout', '300', '--invitation', invitation.decode().strip())
        code, out, err = await finish(joined, 380)
        assert code == 0, err.decode()
        session = re.search(rb'Session: ([a-f0-9]{16})', out)[1].decode()
        assert b' exec ' in out and b'untrusted' not in out and b'nonce' not in out
        assert await execute('printf must-not-run', b'd') == (126, b'', b'Command denied by host.\n')
        assert await execute("printf 'OUT\\n'; printf 'ERR\\n' >&2; exit 17", b'a') == (17, b'OUT\n', b'ERR\n')
        assert await execute('cd /tmp; export RV_EXEC_PROBE=changed; printf approved', b'A') == (0, b'approved', b'')
        offset = len(output)
        assert await execute('printf "%s" "${RV_EXEC_PROBE-unset}"') == (0, b'unset', b'')
        assert await execute('cd /tmp && pwd') == (0, b'/tmp\n', b'')
        assert b'Approve command?' not in output[offset:]
        process = await spawn('session', 'exec', session, 'printf started; sleep 60')
        assert await asyncio.wait_for(process.stdout.readexactly(7), 20) == b'started'
        stopped = time.monotonic()
        os.write(master, b'D')
        code, out, err = await finish(process, 15)
        assert (code, out, err) == (130, b'', b''), (code, out, err)
        await asyncio.wait_for(host.wait(), 15)
        assert host.returncode == 0, output.decode(errors='replace')
        assert termios.tcgetattr(slave) == original
        print(json.dumps(dict(publication_seconds=publication, native_exec_exact_streams=True,
            nonzero_exit_status=17, fresh_shells=True, denied_without_execution=True,
            approved_once=True, approved_all=True, piped_bootstrap=bool(bootstrap),
            host_disconnect_seconds=round(time.monotonic() - stopped, 3))), flush=True)
    finally:
        for process in active:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if session and (Path.home() / '.local/share/rendezvous/sessions' / session / 'control').exists():
            with contextlib.suppress(Exception):
                await finish(await spawn('session', 'close', session))
        if host and host.returncode is None:
            host.send_signal(signal.SIGTERM)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(host.wait(), 15)
            if host.returncode is None:
                host.kill()
                await host.wait()
        collector.cancel()
        await asyncio.gather(collector, return_exceptions=True)
        os.close(master)
        os.close(slave)


if __name__ == '__main__':
    asyncio.run(main())
