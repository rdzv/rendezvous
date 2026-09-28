"""Real-Tor agent sessions with a different CPU architecture at each endpoint."""
import argparse
import asyncio
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid


def guest(expected_host):
    cli = '/bundle/bin/rdzv'
    with tempfile.TemporaryDirectory() as directory:
        invitation = Path(directory) / 'invitation'
        invitation.write_text(sys.stdin.readline())
        invitation.chmod(0o600)
        joined = subprocess.run([cli, 'join', '-a', '--session-timeout', '300',
                                 '--invitation-file', str(invitation)],
                                capture_output=True, timeout=480)
        assert joined.returncode == 0, joined.stderr.decode(errors='replace')
        match = re.search(rb'Session: ([a-f0-9]{16})', joined.stdout)
        assert match, 'No agent session ID returned'
        session = match[1].decode()
        try:
            result = subprocess.run([cli, 'session', 'exec', session, 'uname -m; id -u'],
                                    capture_output=True, timeout=90)
            assert result.returncode == 0, result.stderr.decode(errors='replace')
            assert result.stdout == f'{expected_host}\n10001\n'.encode(), repr(result.stdout)
            result = subprocess.run([cli, 'session', 'exec', session,
                                     "printf 'OUT\\n'; printf 'ERR\\n' >&2; exit 17"],
                                    capture_output=True, timeout=90)
            assert (result.returncode, result.stdout, result.stderr) == (17, b'OUT\n', b'ERR\n'), repr(result)
        finally:
            subprocess.run([cli, 'session', 'close', session], check=True, capture_output=True, timeout=60)
    print('CROSS_PLATFORM_AGENT_PASSED: exact streams, exit 17, remote architecture and uid verified', flush=True)


async def pair(host_arch, guest_arch):
    from release_config import ROOT, PLATFORMS, bundle_path
    names = []
    processes = []
    host_errors = []
    collector = None

    def container(arch):
        name = 'rdzv-cross-' + uuid.uuid4().hex
        names.append(name)
        return ['docker', 'run', '--rm', '-i', '--name', name,
                '--platform', PLATFORMS[arch]['docker'], '--read-only',
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--tmpfs', '/home/guest:exec,uid=10001,gid=10001,mode=700', '--tmpfs', '/tmp',
                '--mount', f'type=bind,src={bundle_path(arch)},dst=/bundle,readonly',
                '--mount', f'type=bind,src={ROOT}/tools/verify_cross_platform.py,dst=/verify.py,readonly',
                f'rendezvous-rootless-test:{arch}']

    async def spawn(command):
        process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        processes.append(process)
        return process

    try:
        host = await spawn([*container(host_arch), '/bundle/bin/rdzv', 'host',
                            '--invite-timeout', '600', '--session-timeout', '300'])

        async def collect_errors():
            while line := await host.stderr.readline():
                host_errors.append(line.decode(errors='replace'))

        collector = asyncio.create_task(collect_errors())
        invitation = await asyncio.wait_for(host.stdout.readline(), 600)
        assert invitation.startswith(b'rv1.'), ''.join(host_errors)
        client = await spawn([*container(guest_arch), '/bundle/python/bin/python3', '-I', '/verify.py',
                              '--guest', '--expected-host', host_arch])
        stdout, stderr = await asyncio.wait_for(client.communicate(invitation), 600)
        assert client.returncode == 0, stderr.decode(errors='replace') + '\nHOST: ' + ''.join(host_errors)
        await asyncio.wait_for(host.wait(), 60)
        assert host.returncode == 0, ''.join(host_errors)
        print(f'{host_arch} host -> {guest_arch} guest: {stdout.decode().strip()}', flush=True)
    finally:
        # Named test containers must also be removed on a timeout or failed check.
        for name in names:
            cleanup = await asyncio.create_subprocess_exec('docker', 'rm', '-f', name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(cleanup.wait(), 30)
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()
        if collector:
            await collector


async def verify():
    from release_config import TOR_NETWORK_TIMEOUTS
    # Every architecture acts as both a host and a guest across CPU boundaries.
    for host_arch, guest_arch in [('x86_64', 'aarch64'), ('aarch64', 'armv7l'), ('armv7l', 'x86_64')]:
        for attempt in range(1, 4):
            try:
                await pair(host_arch, guest_arch)
                break
            except AssertionError as error:
                if attempt == 3 or not any(message in str(error) for message in TOR_NETWORK_TIMEOUTS):
                    raise
                print(f'{host_arch} -> {guest_arch}: {error}\nRetrying Tor network timeout ({attempt + 1}/3)', flush=True)
                await asyncio.sleep(5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--guest', action='store_true')
    parser.add_argument('--expected-host', choices=['x86_64', 'aarch64', 'armv7l'])
    args = parser.parse_args()
    if args.guest:
        guest(args.expected_host)
    else:
        asyncio.run(verify())
