"""Run using the bundled Python inside the unprivileged, read-only test container."""
import asyncio
import os
from pathlib import Path
import signal
import tempfile


async def verify():
    assert os.geteuid() != 0
    cli = str(Path.home() / '.local/bin/rdzv')
    host = await asyncio.create_subprocess_exec(cli, 'host', '--invite-timeout', '600', '--session-timeout', '60', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    client = None
    host_errors = []
    host_connected = asyncio.Event()
    async def collect_errors():
        while line := await host.stderr.readline():
            host_errors.append(line.decode(errors='replace'))
            if line.strip() == b'Connected.':
                host_connected.set()
    collector = asyncio.create_task(collect_errors())
    try:
        invitation = await asyncio.wait_for(host.stdout.readline(), 600)
        assert invitation.startswith(b'rv1.'), ''.join(host_errors)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'invitation'
            path.write_bytes(invitation)
            path.chmod(0o600)
            client = await asyncio.create_subprocess_exec(cli, 'join', '--invitation-file', str(path), '--command', 'id -u; sleep 2', stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            communicate = asyncio.create_task(client.communicate())
            announcement = asyncio.create_task(host_connected.wait())
            done, _ = await asyncio.wait([communicate, announcement], timeout=330, return_when=asyncio.FIRST_COMPLETED)
            if announcement not in done:
                announcement.cancel()
                await asyncio.gather(announcement, return_exceptions=True)
                if communicate in done:
                    _, error = communicate.result()
                    raise AssertionError('Client exited before host connected status: ' + error.decode() + '\nHOST: ' + ''.join(host_errors))
                communicate.cancel()
                await asyncio.gather(communicate, return_exceptions=True)
                raise AssertionError('Timed out waiting for host connected status')
            assert client.returncode is None, 'Host status was delayed until disconnect'
            stdout, stderr = await asyncio.wait_for(communicate, 90)
            assert client.returncode == 0, stderr.decode() + '\nHOST: ' + ''.join(host_errors)
            assert stdout.strip() == str(os.getuid()).encode(), repr(stdout)
        await asyncio.wait_for(host.wait(), 15)
        assert host.returncode == 0, ''.join(host_errors)
        print('ROOTLESS_TOR_SESSION_PASSED: remote uid=' + str(os.getuid()) + '; connected status arrived before disconnect', flush=True)
    finally:
        for process in [client, host]:
            if process and process.returncode is None:
                process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        await collector


asyncio.run(verify())
