"""Real interactive Tor sessions; report startup and keystroke RTT separately."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import pty
import signal
import statistics
import termios
import time


async def session(cli, label, single_hop):
    started = time.monotonic()
    host = await asyncio.create_subprocess_exec(*cli, 'host', '--invite-timeout', '300',
        '--session-timeout', '90', *(['--single-hop'] if single_hop else []),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    errors = bytearray()
    connected = asyncio.Event()
    async def host_output():
        while line := await host.stderr.readline():
            errors.extend(line)
            if line.strip() == b'Connected.':
                connected.set()
    host_reader = asyncio.create_task(host_output())
    child = None
    reader = None
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    flags = fcntl.fcntl(slave, fcntl.F_GETFL)
    os.set_blocking(master, False)
    output = bytearray()
    async def client_output():
        while True:
            try:
                output.extend(os.read(master, 65536))
            except BlockingIOError:
                pass
            await asyncio.sleep(0.005)
    async def expect(value, start=0, timeout=30):
        async def wait():
            while value not in output[start:]:
                if child.returncode is not None:
                    raise AssertionError('Client exited: ' + output.decode(errors='replace'))
                await asyncio.sleep(0.005)
        await asyncio.wait_for(wait(), timeout)
    try:
        invitation = await asyncio.wait_for(host.stdout.readline(), 200)
        assert invitation.startswith(b'rv1.'), errors.decode(errors='replace')
        startup = time.monotonic() - started
        joining = time.monotonic()
        child = await asyncio.create_subprocess_exec(*cli, 'join', '--invitation', invitation.decode().strip(),
            stdin=slave, stdout=slave, stderr=slave, env=dict(os.environ, NO_COLOR=''))
        reader = asyncio.create_task(client_output())
        await expect(b'Connected.', timeout=330)
        await asyncio.wait_for(connected.wait(), 5)
        assert host.returncode is None and child.returncode is None
        # This marker is absent from the echoed input, proving remote execution.
        os.write(master, b"printf '\\122\\104\\132\\126\\137\\122\\105\\101\\104\\131\\n'\n")
        await expect(b'RDZV_READY')
        join_time = time.monotonic() - joining
        samples = []
        for char in b'abcde':
            offset = len(output)
            sent = time.monotonic()
            os.write(master, bytes([char]))
            await expect(bytes([char]), offset)
            samples.append(round((time.monotonic() - sent) * 1000, 1))
        os.write(master, b'\x15exit 23\n')
        await asyncio.wait_for(child.wait(), 30)
        assert child.returncode == 23, output.decode(errors='replace')
        assert termios.tcgetattr(slave) == original
        assert fcntl.fcntl(slave, fcntl.F_GETFL) == flags
        assert b'lost sys.stderr' not in output and b'Traceback' not in output
        await asyncio.wait_for(host.wait(), 15)
        assert host.returncode == 0, errors.decode(errors='replace')
        result = dict(case=label, host_startup_seconds=round(startup, 2),
            join_to_shell_seconds=round(join_time, 2), echo_ms=samples,
            median_echo_ms=statistics.median(samples), terminal_restored=True, exit_status=23)
        print(json.dumps(result), flush=True)
        return result
    finally:
        for process in [child, host]:
            if process and process.returncode is None:
                process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        if reader:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await host_reader
        os.close(master)
        os.close(slave)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cli', nargs='+', default=[str(Path.home() / '.local/bin/rdzv')])
    parser.add_argument('--cases', nargs='+', choices=['cold', 'warm', 'single-hop'], default=['cold', 'warm', 'single-hop'])
    args = parser.parse_args()
    failures = []
    for case in args.cases:
        try:
            await session(args.cli, case, case == 'single-hop')
        except (AssertionError, asyncio.TimeoutError) as error:
            failures.append(case)
            print(json.dumps(dict(case=case, failed=True, error=str(error) or 'Timed out')), flush=True)
    cache = Path.home() / '.local/share/rendezvous/tor-cache'
    files = sorted(str(p.relative_to(cache)) for p in cache.rglob('*') if p.is_file())
    allowed = {'.lease', 'cached-certs', 'cached-consensus', 'cached-microdesc-consensus',
        'cached-microdescs', 'cached-microdescs.new', 'cached-descriptors', 'cached-descriptors.new',
        'unverified-consensus', 'unverified-microdesc-consensus'}
    assert files and all(Path(file).name in allowed for file in files), files
    print(json.dumps(dict(cache_files=files, no_persisted_session_credentials=True)), flush=True)
    if failures:
        raise SystemExit('Failed cases: ' + ', '.join(failures))


if __name__ == '__main__':
    asyncio.run(main())
