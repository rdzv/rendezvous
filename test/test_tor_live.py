"""Opt-in real Tor integration: RUN_TOR_TEST=1 pytest -q test/test_tor_live.py."""
import asyncio
import os
from pathlib import Path
import sys

import pytest


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get('RUN_TOR_TEST') != '1', reason='Requires live Tor network')
async def test_live_host_and_join(tmp_path):
    script = str(Path(__file__).resolve().parents[1] / 'rendezvous.py')
    host = await asyncio.create_subprocess_exec(sys.executable, script, 'host', '--invite-timeout', '300', '--session-timeout', '30', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    client = None
    try:
        invitation = await asyncio.wait_for(host.stdout.readline(), 200)
        assert invitation.startswith(b'rv1.'), (await host.stderr.read()).decode()
        path = tmp_path / 'invitation'
        path.write_bytes(invitation)
        path.chmod(0o600)
        client = await asyncio.create_subprocess_exec(sys.executable, script, 'join', '--invitation-file', str(path), '--command', 'printf live-tor-ok', stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(client.communicate(), 320)
        assert client.returncode == 0, stderr.decode()
        assert b'live-tor-ok' in stdout
        await asyncio.wait_for(host.wait(), 15)
        assert host.returncode == 0, (await host.stderr.read()).decode()
    finally:
        for proc in [client, host]:
            if proc and proc.returncode is None:
                proc.send_signal(__import__('signal').SIGINT)
                try:
                    await asyncio.wait_for(proc.wait(), 10)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
