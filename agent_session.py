"""Private local control socket for native SSH command execution."""
import asyncio
import base64
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import socket
import stat
import struct
import subprocess
import sys
import time

MAX_REQUEST = 65536
DEFAULT_LIFETIME = 43200


def directory(session=None, create=False):
    base = Path.home() / '.local/share/rendezvous'
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths = [base, base / 'sessions']
    if session is not None:
        if not re.fullmatch(r'[a-f0-9]{16}', session):
            raise ValueError('Invalid local session ID.')
        paths.append(paths[-1] / session)
    for index, path in enumerate(paths):
        if index < 2 or create:
            path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError('Local session directory is not private.')
    if len(os.fsencode(paths[-1] / 'control')) >= 104:
        raise RuntimeError('Home path is too long for the local session socket.')
    return paths[-1]


def cli_command():
    root = Path(__file__).resolve().parent
    bundled = root.parent / 'bin/rdzv'
    return [str(bundled)] if root.name == 'app' and bundled.is_file() else [sys.executable, str(root / 'rendezvous.py')]


def guide(session, state='connected', lifetime=DEFAULT_LIFETIME):
    prefix = shlex.join(cli_command()) + ' session'
    return (f'{state.capitalize()}. Session: {session}\n'
            f"  {prefix} exec {session} 'pwd'\n"
            f'  {prefix} help {session}\n'
            f'  {prefix} close {session}\n'
            'Exec returns stdout, stderr, and the command exit code in one call.\n'
            'Each command uses a fresh shell; use cd /path && ... when needed.\n'
            f'Session limit: {lifetime} seconds. Close when finished; no reconnection.\n')


def write_state(path, state, error=''):
    pending = path / 'state.tmp'
    pending.write_text(json.dumps({'state': state, 'error': error}))
    os.replace(pending, path / 'state.json')


def launch_worker(session, secret_input, log):
    command = [sys.executable, '-I', str(Path(__file__).with_name('rendezvous.py')), '_agent-worker', session]
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=log, start_new_session=True)
    child.stdin.write(json.dumps(secret_input).encode())
    child.stdin.close()
    return child


async def start(invitation, lifetime=DEFAULT_LIFETIME):
    session = secrets.token_hex(8)
    path = directory(session, create=True)
    child = None
    ready = False
    try:
        write_state(path, 'connecting')
        print(f'Connecting. Session: {session}', flush=True)
        with (path / 'startup.log').open('xb') as log:
            child = launch_worker(session, {'invitation': invitation, 'session_timeout': lifetime}, log)
        deadline = time.monotonic() + 380
        while time.monotonic() < deadline:
            state = json.loads((path / 'state.json').read_text())
            if state['state'] == 'connected':
                ready = True
                print(guide(session, lifetime=lifetime), end='', flush=True)
                return 0
            if state['state'] in ('failed', 'closed') or child.poll() is not None:
                raise RuntimeError(state.get('error') or 'Agent session ended before it was ready.')
            await asyncio.sleep(0.1)
        raise RuntimeError('Agent connection startup timed out.')
    finally:
        if not ready and child and child.poll() is None:
            child.terminate()
            for _ in range(100):
                if child.poll() is not None:
                    break
                await asyncio.sleep(0.05)
            if child.poll() is None:
                child.kill()
                await asyncio.to_thread(child.wait)


class Session:
    def __init__(self, session, path, lifetime=DEFAULT_LIFETIME):
        self.session, self.path, self.lifetime = session, path, lifetime
        self.state = 'connecting'
        self.conn = None
        self.exec_lock = asyncio.Lock()
        self.exec_finished = asyncio.Event()
        self.exec_finished.set()
        self.stop = asyncio.Event()
        self.clients = set()

    def set_state(self, state, error=''):
        self.state = state
        write_state(self.path, state, error)

    async def frame(self, writer, message):
        # Local wire framing only. The CLI writes decoded bytes to the real streams.
        writer.write(json.dumps(message).encode() + b'\n')
        await asyncio.wait_for(writer.drain(), 10)

    async def execute(self, request, reader, writer):
        command = request.get('command')
        if not isinstance(command, str) or '\x00' in command or len(command.encode()) > MAX_REQUEST // 2:
            raise ValueError('Command must be text of at most 32768 bytes without NUL.')
        async with self.exec_lock:
            if reader.at_eof() or writer.is_closing():
                return
            if self.state != 'connected' or not self.conn or self.stop.is_set():
                raise RuntimeError('Session is not connected.')
            self.exec_finished.clear()
            try:
                async with self.conn.create_process(command, request_pty=False, stdin=asyncio.subprocess.DEVNULL) as process:
                    async def forward(stream, name):
                        while data := await stream.read(32768):
                            await self.frame(writer, {'type': name, 'data': base64.b64encode(data).decode()})
                    async def result():
                        await asyncio.gather(forward(process.stdout, 'stdout'), forward(process.stderr, 'stderr'))
                        await process.wait_closed()
                        code = process.exit_status
                        if code is None or code < 0:
                            code = 128 + getattr(signal, 'SIG' + process.exit_signal[0], 0) if process.exit_signal else 1
                        return code
                    completed = asyncio.create_task(result())
                    # A killed local tool call closes this socket; stop its remote job.
                    disconnected = asyncio.create_task(reader.read(1))
                    stopped = asyncio.create_task(self.stop.wait())
                    tasks = [completed, disconnected, stopped]
                    try:
                        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        if completed in done:
                            await self.frame(writer, {'type': 'exit', 'status': await completed})
                        else:
                            process.close()
                            if stopped in done and disconnected not in done:
                                await self.frame(writer, {'type': 'exit', 'status': 130})
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                self.exec_finished.set()

    async def client(self, reader, writer):
        task = asyncio.current_task()
        self.clients.add(task)
        try:
            peer = writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if struct.unpack('3i', peer)[1] != os.getuid() or len(self.clients) > 32:
                return
            try:
                request = json.loads(await asyncio.wait_for(reader.readline(), 5))
                if not isinstance(request, dict):
                    raise ValueError('Expected a request object.')
                if request.get('op') == 'exec':
                    await self.execute(request, reader, writer)
                elif request.get('op') == 'help':
                    await self.frame(writer, {'instructions': guide(self.session, self.state, self.lifetime)})
                elif request.get('op') == 'close':
                    await self.frame(writer, {'state': 'closing'})
                    self.stop.set()
                else:
                    raise ValueError('Unknown session operation. Use session exec, help, or close.')
            except Exception as exc:
                with contextlib.suppress(OSError, asyncio.TimeoutError):
                    await self.frame(writer, {'error': str(exc) or 'Session operation failed.'})
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (OSError, asyncio.TimeoutError):
                writer.transport.abort()
            self.clients.discard(task)

    async def bridge(self, invitation, connection_factory):
        try:
            async with connection_factory(invitation) as conn:
                try:
                    control = await asyncio.wait_for(conn.create_process(subsystem='rendezvous-agent', request_pty=False), 10)
                except asyncio.TimeoutError as exc:
                    raise RuntimeError('Host did not open the agent control channel.') from exc
                async with control:
                    try:
                        ready = await asyncio.wait_for(control.stdout.readline(), 10)
                    except asyncio.TimeoutError as exc:
                        raise RuntimeError('Host does not support agent exec; start an updated host session.') from exc
                    if ready != b'RDZV-EXEC 1\n':
                        raise RuntimeError('Host does not support agent exec; start an updated host session.')
                    self.conn = conn
                    self.set_state('connected')
                    stopped = asyncio.create_task(self.stop.wait())
                    ended = asyncio.create_task(control.wait_closed())
                    try:
                        done, _ = await asyncio.wait([stopped, ended], timeout=self.lifetime, return_when=asyncio.FIRST_COMPLETED)
                        self.state = 'closing'
                        if ended not in done:
                            self.stop.set()
                        # Let an active command deliver its last output/status before
                        # acknowledging the host's control-channel shutdown.
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(self.exec_finished.wait(), 5)
                    finally:
                        for task in [stopped, ended]:
                            task.cancel()
                        await asyncio.gather(stopped, ended, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.set_state('failed', str(exc) or 'Connection failed.')
        else:
            self.set_state('closed')
        finally:
            self.conn = None


async def worker(session, invitation, connection_factory, lifetime=DEFAULT_LIFETIME):
    if lifetime <= 0:
        raise ValueError('Session timeout must be positive.')
    path = directory(session)
    state = Session(session, path, lifetime)
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, state.stop.set)
    server = await asyncio.start_unix_server(state.client, path=str(path / 'control'), limit=MAX_REQUEST)
    os.chmod(path / 'control', 0o600)
    bridge = asyncio.create_task(state.bridge(invitation, connection_factory))
    stop = asyncio.create_task(state.stop.wait())
    try:
        done, _ = await asyncio.wait([bridge, stop], timeout=lifetime, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            state.stop.set()
        if state.stop.is_set() and not bridge.done():
            try:
                await asyncio.wait_for(asyncio.shield(bridge), 12)
            except asyncio.TimeoutError:
                bridge.cancel()
    finally:
        state.stop.set()
        stop.cancel()
        bridge.cancel()
        await asyncio.gather(stop, bridge, return_exceptions=True)
        server.close()
        await server.wait_closed()
        for task in list(state.clients):
            task.cancel()
        await asyncio.gather(*state.clients, return_exceptions=True)
        with contextlib.suppress(FileNotFoundError):
            (path / 'control').unlink()
        if state.state != 'failed':
            write_state(path, 'closed')
        loop.remove_signal_handler(signal.SIGTERM)


def local_request(session, request, stream_output=False):
    path = directory(session)
    control = path / 'control'
    info = control.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('Local session socket is not private.')
    body = json.dumps(request).encode() + b'\n'
    if len(body) > MAX_REQUEST:
        raise ValueError('Local session request is too large.')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(str(control))
        if struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1] != os.getuid():
            raise RuntimeError('Unexpected local session owner.')
        sock.sendall(body)
        if stream_output:
            sock.settimeout(None)  # The daemon enforces the session deadline.
        with sock.makefile('rb') as stream:
            while True:
                reply = stream.readline(MAX_REQUEST)
                if not reply.endswith(b'\n'):
                    raise RuntimeError('Local session closed before returning a result.')
                result = json.loads(reply)
                if result.get('error'):
                    raise RuntimeError(result['error'])
                if not stream_output:
                    return result
                if result.get('type') in ('stdout', 'stderr'):
                    target = sys.stdout.buffer if result['type'] == 'stdout' else sys.stderr.buffer
                    target.write(base64.b64decode(result['data'], validate=True))
                    target.flush()
                elif result.get('type') == 'exit' and type(result.get('status')) is int:
                    return result['status']
                else:
                    raise RuntimeError('Invalid command response.')


def cli(args):
    if args.action == 'exec':
        return local_request(args.session, {'op': 'exec', 'command': args.command}, stream_output=True)
    result = local_request(args.session, {'op': args.action})
    if args.action == 'help':
        print(result['instructions'], end='')
    return 0
