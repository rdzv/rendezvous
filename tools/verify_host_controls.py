"""Live Tor verification of host logging and D/Ctrl-C disconnect latency."""
import asyncio
import fcntl
import json
import os
from pathlib import Path
import pty
import signal
import sys
import termios
import time


async def session(key, interactive):
    cli = str(Path.home() / '.local/bin/rdzv')
    host_master, host_slave = pty.openpty()
    guest_master, guest_slave = pty.openpty()
    host_settings = termios.tcgetattr(host_slave)
    termios.tcsetwinsize(host_slave, (24, 100))
    guest_settings = termios.tcgetattr(guest_slave)
    guest_flags = fcntl.fcntl(guest_slave, fcntl.F_GETFL)
    for fd in [host_master, guest_master]:
        os.set_blocking(fd, False)
    host_output, guest_output = bytearray(), bytearray()
    async def collect(fd, output):
        while True:
            try:
                output.extend(os.read(fd, 65536))
            except BlockingIOError:
                pass
            await asyncio.sleep(0.005)
    async def expect(output, text, process, timeout):
        async def wait():
            while text not in output:
                assert process.returncode is None, output.decode(errors='replace')
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), timeout)
    readers = [asyncio.create_task(collect(host_master, host_output)),
               asyncio.create_task(collect(guest_master, guest_output))]
    host = guest = None
    try:
        launcher = f'import os,fcntl,termios,sys; os.setsid(); fcntl.ioctl({host_slave},termios.TIOCSCTTY,0); os.execv(sys.argv[1],sys.argv[1:])'
        bootstrap = os.environ.get('HOST_BOOTSTRAP')
        host_command = ['/bin/sh', '-s', '--', 'host'] if bootstrap else [cli, 'host']
        host = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', launcher,
            *host_command, '--single-hop', '--invite-timeout', '300',
            *(['--verbose'] if interactive else []), pass_fds=(host_slave,),
            stdin=asyncio.subprocess.PIPE if bootstrap else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=host_slave,
            env=dict(os.environ, NO_COLOR=''))
        if bootstrap:
            host.stdin.write(Path(bootstrap).read_bytes())
            await host.stdin.drain()
            host.stdin.close()
        invitation = await asyncio.wait_for(host.stdout.readline(), 200)
        assert invitation.startswith(b'rv1.'), host_output.decode(errors='replace')
        command = "printf 'OUT-%s\\n' READY; printf 'ERR-%s\\n' READY >&2; sleep 60"
        guest = await asyncio.create_subprocess_exec(cli, 'join', '--invitation', invitation.decode().strip(),
            *([] if interactive else ['--command', command]),
            stdin=guest_slave, stdout=guest_slave, stderr=guest_slave, env=dict(os.environ, NO_COLOR=''))
        await expect(guest_output, b'Connected.', guest, 330)
        if interactive:
            os.write(guest_master, command.encode() + b'\n')
        await expect(guest_output, b'ERR-READY', guest, 30)
        assert b'OUT-READY' in guest_output
        await expect(host_output, b'[input]' if interactive else b'[command]', host, 5)
        assert b'[output]' in host_output
        if interactive:
            assert b'ERR-READY' in host_output
        start = time.monotonic()
        os.write(host_master, key)
        await asyncio.wait_for(guest.wait(), 12)
        elapsed = time.monotonic() - start
        await asyncio.sleep(0.05)
        assert guest.returncode == 130, guest_output.decode(errors='replace')
        assert b'Session ended by host.' in guest_output
        assert termios.tcgetattr(guest_slave) == guest_settings
        assert fcntl.fcntl(guest_slave, fcntl.F_GETFL) == guest_flags
        await asyncio.wait_for(host.wait(), 12)
        assert host.returncode == 0, host_output.decode(errors='replace')
        assert termios.tcgetattr(host_slave) == host_settings
        print(json.dumps(dict(control='Ctrl-C' if key == b'\x03' else key.decode(), interactive=interactive,
            piped_bootstrap=bool(bootstrap),
            joiner_exit_seconds=round(elapsed, 3), guest_notified=True,
            host_and_guest_terminals_restored=True, logging_verified=True)), flush=True)
    except BaseException:
        # Invitations are on the host's separate stdout pipe, never this log.
        print('HOST CONSOLE: ' + host_output.decode(errors='replace'), flush=True)
        raise
    finally:
        for process in [guest, host]:
            if process and process.returncode is None:
                process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 15)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        for fd in [host_master, host_slave, guest_master, guest_slave]:
            os.close(fd)


async def main():
    for control in os.environ.get('CONTROL_CASES', 'Ctrl-C,D').split(','):
        key = {'Ctrl-C': b'\x03', 'D': b'D'}[control]
        await session(key, control == 'Ctrl-C')


if __name__ == '__main__':
    asyncio.run(main())
