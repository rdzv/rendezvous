"""Linux-only prototype. No public infrastructure or persistent enrollment state."""
import argparse
import asyncio
import base64
import codecs
import contextlib
import fcntl
import hmac
import importlib.util
import json
import os
from pathlib import Path
import pty
import re
import secrets
import shutil
import signal
import stat
import struct
import sys
import tempfile
import termios
import time
import tty

import asyncssh
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption


def paint(text, code, stream):
    if stream.isatty() and 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb':
        return f'\033[{code}m{text}\033[0m'
    return text


def status(message, code='90'):
    print(paint(message, code, sys.stderr), file=sys.stderr, flush=True)


class HostLog:
    """A terminal-safe transcript, not a shell parser or command approval boundary."""
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.pending = bytearray()
        self.out = bytearray()
        self.out_truncated = False
        self.prompt = False
        self.marked_prompt = False
        self.escape = None
        self.csi = bytearray()
        self.last_cr = {'input': False, 'output': False}
        self.open_line = None
        self.input_decoder = codecs.getincrementaldecoder('utf-8')(errors='backslashreplace')

    @staticmethod
    def safe(data):
        return json.dumps(data.decode('utf-8', errors='backslashreplace'), ensure_ascii=True)[1:-1]

    def command(self, command):
        self.flush()
        self.emit('command', command.encode())

    def emit(self, kind, data, end=True, truncated=False):
        # Long records can stream in bounded fragments without adding line breaks.
        if self.open_line != kind:
            if self.open_line:
                sys.stderr.write('\n')
            sys.stderr.write(f'[{kind}]'.ljust(9) + ' ')
        sys.stderr.write(paint(self.safe(data), '31' if kind in ('input', 'command') else '36', sys.stderr))
        if truncated:
            sys.stderr.write(paint(' ...', '36', sys.stderr))
        sys.stderr.write('\n' if end else '')
        sys.stderr.flush()
        self.open_line = None if end else kind

    @staticmethod
    def plain_prompt(data):
        return bool(re.fullmatch(rb'(?:[^\r\n]{0,160}[@:~][^\r\n]{0,160})?[$#>] ', data))

    def input(self, data):
        if not self.verbose and self.plain_prompt(bytes(self.out)):
            self.out.clear()
            self.prompt = True
            self.marked_prompt = False
        for char in data:
            if char in (10, 13):
                if char == 10 and self.last_cr['input']:
                    self.last_cr['input'] = False
                    continue
                tail = bytes(self.pending) if self.verbose else self.input_decoder.decode(b'', final=True).encode()
                self.emit('input', tail)
                self.input_decoder.reset()
                self.pending.clear()
                self.last_cr['input'] = char == 13
            else:
                self.last_cr['input'] = False
                if self.verbose:
                    self.pending.append(char)
                    if len(self.pending) >= 4096:
                        self.emit('input', bytes(self.pending), end=False)
                        self.pending.clear()
                else:
                    text = self.input_decoder.decode(bytes([char]))
                    if text:
                        self.emit('input', text.encode(), end=False)

    def end_output(self, force=False, hide_prompt=False):
        if self.out or self.open_line == 'output' or force:
            if self.verbose or not (hide_prompt and self.plain_prompt(bytes(self.out))):
                self.emit('output', bytes(self.out), truncated=self.out_truncated)
        self.out.clear()
        self.out_truncated = False

    def flush(self):
        if self.pending or self.open_line == 'input' or self.input_decoder.getstate()[0]:
            tail = bytes(self.pending) if self.verbose else self.input_decoder.decode(b'', final=True).encode()
            self.emit('input', tail)
            self.input_decoder.reset()
            self.pending.clear()
        self.end_output(hide_prompt=True)

    def output(self, data):
        for char in data:
            if not self.verbose:
                # Stateful parsing matters: SSH can split any escape sequence.
                if self.escape == 'esc':
                    self.escape = ('csi' if char == 91 else 'string' if char in (93, 80, 88, 94, 95)
                                   else 'intermediate' if 0x20 <= char <= 0x2f else None)
                    self.csi.clear()
                    continue
                if self.escape == 'intermediate':
                    if not 0x20 <= char <= 0x2f:
                        self.escape = None
                    continue
                if self.escape == 'csi':
                    if len(self.csi) < 64:
                        self.csi.append(char)
                    if 0x40 <= char <= 0x7e:
                        sequence = bytes(self.csi)
                        if sequence == b'?2004h':
                            self.end_output(hide_prompt=True)
                            self.prompt = self.marked_prompt = True
                        elif sequence == b'?2004l':
                            self.prompt = self.marked_prompt = False
                        self.escape = None
                    continue
                if self.escape in ('string', 'string-esc'):
                    if char == 7 or (self.escape == 'string-esc' and char == 92):
                        self.escape = None
                    else:
                        self.escape = 'string-esc' if char == 27 else 'string'
                    continue
                if char == 27:
                    self.escape = 'esc'
                    continue
                if self.prompt:
                    if char in (10, 13) and not self.marked_prompt:
                        self.prompt = False
                    continue
            if char in (10, 13):
                if char == 10 and self.last_cr['output']:
                    self.last_cr['output'] = False
                    continue
                self.end_output(force=self.verbose)
                self.last_cr['output'] = char == 13
                continue
            self.last_cr['output'] = False
            if self.verbose:
                self.out.append(char)
                if len(self.out) >= 4096:
                    self.emit('output', bytes(self.out), end=False)
                    self.out.clear()
            else:
                if self.out_truncated:
                    continue
                width = max(1, shutil.get_terminal_size().columns - 10 - 4)
                if len(self.safe(bytes(self.out) + bytes([char]))) <= width:
                    self.out.append(char)
                else:
                    self.out_truncated = True


class HostStatusBar:
    """Reserve the terminal's bottom row; logs scroll above the local controls."""
    def __init__(self):
        self.rows = 0
        self.columns = 0
        self.pending = False
        self.active = sys.stderr.isatty() and os.environ.get('TERM') != 'dumb'

    def label(self):
        bindings = [('a', 'approve', '32'), ('A', 'approve all', '32'), ('d', 'deny', '31')] if self.pending else []
        bindings.append(('D', 'disconnect', '31'))
        text, colors = '', {}
        for key, action, color in bindings:
            if text:
                text += ', '
            colors[len(text)] = color
            text += f'{key}: {action}'
        return ''.join(paint(char, colors[index], sys.stderr) if index in colors else char
                       for index, char in enumerate(text[:max(0, self.columns - 1)]))

    def redraw(self):
        if self.rows:
            sys.stderr.write(f'\0337\033[{self.rows};1H\033[2K{self.label()}\0338')
            sys.stderr.flush()

    def resize(self):
        if not self.active:
            return
        columns, rows = os.get_terminal_size(sys.stderr.fileno())
        if rows < 2 or columns < 2:
            self.close()
            return
        if self.rows:
            sys.stderr.write(f'\033[{min(self.rows, rows)};1H\033[2K')
        else:
            # Scroll the old bottom line into the log area instead of erasing it.
            sys.stderr.write(f'\033[{rows};1H\n')
        self.rows = rows
        self.columns = columns
        label = self.label()
        sys.stderr.write(f'\033[1;{rows - 1}r\033[{rows};1H\033[2K{label}\033[{rows - 1};1H')
        sys.stderr.flush()

    def close(self):
        if self.rows:
            sys.stderr.write(f'\033[r\033[{self.rows};1H\033[2K')
            sys.stderr.flush()
            self.rows = 0


class CommandApproval:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.available = False
        self.pending = None
        self.bar = None
        self.bad_keys = 0
        self.expanded = False
        self.prompt_open = False

    def key(self, char):
        if self.pending is None or self.pending.done():
            return
        if char in (ord('a'), ord('A'), ord('d')):
            sys.stderr.write(paint(chr(char), '31' if char == ord('d') else '32', sys.stderr) + '\n')
            sys.stderr.flush()
            self.prompt_open = False
            if char == ord('A'):
                self.enabled = False
            self.pending.set_result(char != ord('d'))
        else:
            self.bad_keys += 1
            if self.bad_keys >= 2 and not self.expanded:
                sys.stderr.write('\nApprove command (a/A/d)? ')
                sys.stderr.flush()
                self.expanded = True

    async def request(self, process):
        if not self.enabled:
            return True
        if not self.available:
            return False
        self.pending = asyncio.get_running_loop().create_future()
        self.bad_keys = 0
        self.expanded = False
        self.prompt_open = True
        if self.bar:
            self.bar.pending = True
            self.bar.redraw()
        sys.stderr.write('Approve command? ')
        sys.stderr.flush()
        closed = asyncio.create_task(process.wait_closed())
        try:
            done, _ = await asyncio.wait([self.pending, closed], return_when=asyncio.FIRST_COMPLETED)
            return self.pending in done and not process.is_closing() and self.pending.result()
        finally:
            closed.cancel()
            await asyncio.gather(closed, return_exceptions=True)
            if not self.pending.done():
                self.pending.cancel()
            self.pending = None
            if self.prompt_open:
                sys.stderr.write('\n')
                sys.stderr.flush()
                self.prompt_open = False
            if self.bar:
                self.bar.pending = False
                self.bar.redraw()


@contextlib.contextmanager
def host_controls(approval=None):
    """Read local controls from the terminal, never from curl's installer pipe."""
    try:
        fd = os.open('/dev/tty', os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        if approval and approval.enabled:
            raise RuntimeError('--approve requires a controlling terminal.')
        yield
        return
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    old = None
    bar = HostStatusBar()
    if approval:
        approval.available = True
        approval.bar = bar
    try:
        old = termios.tcgetattr(fd)
        tty.setcbreak(fd)  # Keep ISIG: Ctrl-C continues to use asyncio's SIGINT handler.
        escape = None
        escape_started = 0.0
        def read_keys():
            nonlocal escape, escape_started
            try:
                data = os.read(fd, 1024)
            except BlockingIOError:
                return
            except OSError:
                data = b''
            disconnect = not data
            for char in data:
                if escape and time.monotonic() - escape_started > 0.25:
                    escape = None
                if escape == 'prefix':
                    escape = 'sequence' if char in (ord('['), ord('O')) else None
                elif escape == 'sequence':
                    if 0x40 <= char <= 0x7e:
                        escape = None
                elif char == 27:
                    escape = 'prefix'
                    escape_started = time.monotonic()
                elif char == ord('D'):
                    disconnect = True
                elif approval:
                    approval.key(char)
            if disconnect:
                loop.remove_reader(fd)
                task.cancel()
        loop.add_reader(fd, read_keys)
        bar.resize()
        if bar.active:
            loop.add_signal_handler(signal.SIGWINCH, bar.resize)
        yield
    finally:
        if approval:
            approval.available = False
        loop.remove_reader(fd)
        if bar.active:
            loop.remove_signal_handler(signal.SIGWINCH)
        try:
            bar.close()
        finally:
            try:
                if old is not None:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
            finally:
                os.close(fd)


def ssh_stdin(stream):
    info = os.fstat(stream.fileno())
    if stat.S_ISCHR(info.st_mode) and info.st_rdev == os.stat(os.devnull).st_rdev:
        # /dev/null cannot be registered with epoll as an interactive read pipe.
        return asyncssh.DEVNULL
    return stream


@contextlib.contextmanager
def terminal_streams():
    """AsyncSSH owns redirects; never give it process-global standard streams."""
    streams = [sys.stdin, sys.stdout, sys.stderr]
    sys.stdout.flush()
    sys.stderr.flush()
    flags = [(stream.fileno(), fcntl.fcntl(stream.fileno(), fcntl.F_GETFL)) for stream in streams]
    terminal = termios.tcgetattr(flags[0][0]) if sys.stdin.isatty() else None
    try:
        with contextlib.ExitStack() as owned:
            copies = [owned.enter_context(os.fdopen(os.dup(fd), mode, buffering=0))
                      for (fd, _), mode in zip(flags, ['rb', 'wb', 'wb'])]
            if terminal is not None:
                tty.setraw(flags[0][0])
            yield ssh_stdin(copies[0]), copies[1], copies[2]
    finally:
        # dup shares file-status flags with the original (and the parent shell).
        # Restore only after AsyncSSH's process/transport cleanup has completed.
        try:
            if terminal is not None:
                termios.tcsetattr(flags[0][0], termios.TCSADRAIN, terminal)
        finally:
            for fd, original in flags:
                fcntl.fcntl(fd, fcntl.F_SETFL, original)


def b32(value):
    return base64.b32encode(value).decode().rstrip('=')


def encode_invitation(data):
    return 'rv1.' + base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')


def decode_invitation(text):
    if len(text) > 4096 or not text.startswith('rv1.'):
        raise ValueError('Invalid invitation')
    data = json.loads(base64.b64decode(text[4:] + '=' * (-len(text[4:]) % 4), altchars=b'-_', validate=True))
    if not isinstance(data, dict):
        raise ValueError('Invalid invitation object')
    for field, pattern in {
        'onion': r'[a-z2-7]{56}\.onion',
        'auth': r'[A-Z2-7]{52}',
        'secret': r'[A-Za-z0-9_-]{43}',
        'fingerprint': r'SHA256:[A-Za-z0-9+/]{43}',
    }.items():
        if not isinstance(data.get(field), str) or not re.fullmatch(pattern, data[field]):
            raise ValueError('Invalid invitation field: ' + field)
    if type(data.get('expires')) is not int or data['expires'] <= time.time():
        raise ValueError('Invitation expired')
    return data


class Gate:
    """Synchronous claim on one asyncio loop: no await between check and consume."""
    def __init__(self, secret, deadline):
        self.secret = secret
        self.deadline = deadline
        self.owner = None
        self.connections = set()
        self.channel_used = False
        self.claimed = asyncio.Event()
        self.finished = asyncio.Event()
        self.process = None
        self.log = None
        self.agent_mode = False
        self.control_process = None
        self.exec_lock = asyncio.Lock()
        self.approval = None

    def claim(self, owner, username, password):
        if self.owner is not None or time.monotonic() >= self.deadline:
            return False
        if username != 'rendezvous' or not hmac.compare_digest(password.encode(), self.secret.encode()):
            return False
        self.owner = owner
        self.secret = ''
        self.claimed.set()
        return True


class Server(asyncssh.SSHServer):
    def __init__(self, gate):
        self.gate = gate
        self.attempts = 0

    def connection_made(self, conn):
        self.conn = conn
        self.gate.connections.add(conn)

    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        self.attempts += 1
        if self.attempts > 3:
            self.conn.abort()
            return False
        return self.gate.claim(self, username, password)

    def connection_lost(self, exc):
        self.gate.connections.discard(self.conn)
        if self.gate.owner is self:
            self.gate.finished.set()


async def fd_write(fd, data):
    loop = asyncio.get_running_loop()
    while data:
        try:
            n = os.write(fd, data)
            data = data[n:]
        except BlockingIOError:
            ready = loop.create_future()
            loop.add_writer(fd, lambda: None if ready.done() else ready.set_result(None))
            try:
                await ready
            finally:
                loop.remove_writer(fd)


async def shell_session(process, gate):
    if gate.agent_mode:
        if process.command is None or process.subsystem or process.term_type:
            process.exit(1)
            return
        async with gate.exec_lock:
            if not process.is_closing() and not gate.control_process.is_closing():
                await exec_session(process, gate)
        return
    if gate.channel_used:
        process.exit(1)
        return
    gate.channel_used = True
    if process.subsystem == 'rendezvous-agent':
        gate.agent_mode = True
        gate.control_process = process
        process.stdout.write(b'RDZV-EXEC 1\n')
        try:
            await process.wait_closed()
        finally:
            gate.finished.set()
        return
    if process.subsystem:
        process.exit(1)
        gate.finished.set()
        return
    if gate.approval and gate.approval.enabled:
        process.stderr.write(b'Command approval requires agent mode (-a).\n')
        process.exit(126)
        gate.finished.set()
        return
    gate.process = process
    if gate.log and process.command:
        gate.log.command(process.command)
    master, slave = pty.openpty()
    child = None
    tasks = []
    loop = asyncio.get_running_loop()
    os.set_blocking(master, False)

    def resize(width, height, *_):
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', max(1, min(height, 65535)), max(1, min(width, 65535)), 0, 0))

    try:
        resize(*(process.term_size or (80, 24, 0, 0)))
        shell = os.environ.get('SHELL', '/bin/sh')
        env = dict(os.environ, TERM=process.term_type or 'xterm-256color')
        # setsid + TIOCSCTTY provides proper job control, without unsafe preexec_fn.
        launcher = 'import os,fcntl,termios; os.setsid(); fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execvpe(os.environ["RV_SHELL"], [os.environ["RV_SHELL"]]+__import__("sys").argv[1:], {k:v for k,v in os.environ.items() if k!="RV_SHELL"})'
        args = ['-c', process.command] if process.command else ['-i']
        child = await asyncio.create_subprocess_exec(sys.executable, '-c', launcher, *args, stdin=slave, stdout=slave, stderr=slave, env=dict(env, RV_SHELL=shell))
        os.close(slave)
        slave = -1

        async def output():
            while True:
                ready = loop.create_future()
                loop.add_reader(master, lambda: None if ready.done() else ready.set_result(None))
                try:
                    await ready
                finally:
                    loop.remove_reader(master)
                try:
                    data = os.read(master, 65536)
                except OSError:
                    return
                if not data:
                    return
                if gate.log:
                    gate.log.output(data)
                process.stdout.write(data)
                await process.stdout.drain()

        async def input_():
            while True:
                try:
                    data = await process.stdin.read(65536)
                except asyncssh.TerminalSizeChanged as change:
                    resize(change.width, change.height)
                    continue
                if not data:
                    await fd_write(master, b'\x04')
                    return
                if gate.log:
                    gate.log.input(data)
                await fd_write(master, data)

        tasks = [asyncio.create_task(output()), asyncio.create_task(input_()), asyncio.create_task(process.wait_closed())]
        done, _ = await asyncio.wait([tasks[0], tasks[2]], return_when=asyncio.FIRST_COMPLETED)
        if tasks[2] in done:
            return
        await tasks[0]
        status = await child.wait()
        process.exit(status if status >= 0 else 128 - status)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if child:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            await child.wait()
        loop.remove_reader(master)
        os.close(master)
        if slave >= 0:
            os.close(slave)
        if gate.log:
            gate.log.flush()
        gate.finished.set()


async def exec_session(process, gate):
    """One native SSH exec request: separate pipes, no PTY or interactive prompt."""
    gate.process = process
    child = None
    tasks = []
    completed = False
    try:
        if gate.log:
            gate.log.flush()
            gate.log.emit('input', process.command.encode())
        if gate.approval and not await gate.approval.request(process):
            if not process.is_closing():
                process.stderr.write(b'Command denied by host.\n')
                process.exit(126)
            return
        child = await asyncio.create_subprocess_exec(os.environ.get('SHELL', '/bin/sh'), '-c', process.command,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        async def forward(source, destination):
            while data := await source.read(32768):
                if gate.log:
                    gate.log.output(data)
                destination.write(data)
                await destination.drain()
        async def run():
            await asyncio.gather(forward(child.stdout, process.stdout), forward(child.stderr, process.stderr))
            return await child.wait()
        running = asyncio.create_task(run())
        disconnected = asyncio.create_task(process.wait_closed())
        tasks = [running, disconnected]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if running in done and not process.is_closing():
            result = await running
            process.exit(result if result >= 0 else 128 - result)
            completed = True
    finally:
        if child and not completed:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if child:
            await child.wait()
        if gate.log:
            gate.log.flush()
        gate.process = None


async def close_host_ssh(server, gate):
    """Finish the channel and let the guest close SSH while Tor is still alive."""
    server.close()
    owner = gate.owner.conn if gate.owner else None
    if owner and not owner.is_closed() and (gate.process or gate.control_process) and not gate.finished.is_set():
        with contextlib.suppress(asyncssh.Error, OSError):
            if gate.process:
                if not gate.agent_mode:
                    gate.process.stderr.write(b'\r\nSession ended by host.\r\n')
                gate.process.exit(130)
            if gate.control_process:
                gate.control_process.exit(130)
        try:
            # Unlike local transport closure, the peer's SSH close proves it had
            # a chance to receive the exit status before we remove its Tor route.
            await asyncio.wait_for(owner.wait_closed(), 5)
        except asyncio.TimeoutError:
            pass
    connections = list(gate.connections)
    for conn in connections:
        conn.close()
    try:
        await asyncio.wait_for(asyncio.gather(*(conn.wait_closed() for conn in connections)), 5)
    except asyncio.TimeoutError:
        for conn in connections:
            conn.abort()
    await asyncio.wait_for(server.wait_closed(), 5)
    if gate.log:
        gate.log.flush()


@contextlib.contextmanager
def tor_cache(root):
    """Lease a private directory-only cache; simultaneous Tor processes never share it."""
    base = Path.home() / '.local/share/rendezvous'
    descriptors = []
    lease = None
    selected = root / 'cache'
    try:
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        for name in [None, 'tor-cache']:
            if name is not None:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                descriptors.append(fd)
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise OSError('Cache directory is not private')
        for index in range(8):
            name = f'slot-{index}'
            try:
                os.mkdir(name, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            slot = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(slot)
            info = os.fstat(slot)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                continue
            lock = os.open('.lease', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=slot)
            info = os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                os.close(lock)
                continue
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(lock)
                continue
            lease = lock
            selected = base / 'tor-cache' / name
            break
    except OSError:
        # Read-only/unsafe caches must never prevent a fresh private session.
        pass
    try:
        yield selected, lease
    finally:
        if lease is not None:
            os.close(lease)
        for fd in reversed(descriptors):
            os.close(fd)


def tor_path(path):
    # Tor quoted-string syntax, including home directories containing whitespace.
    return json.dumps(str(path), ensure_ascii=False)


@contextlib.asynccontextmanager
async def tor(root, extra):
    with tor_cache(root) as (cache, lease):
        async with tor_process(root, extra, cache, lease) as process:
            yield process


@contextlib.asynccontextmanager
async def tor_process(root, extra, cache, lease):
    executable = shutil.which('tor')
    if not executable:
        raise RuntimeError('Tor runtime missing. Run the website command to refresh the installation.')
    config = root / 'torrc'
    publishing = (root / 'service').is_dir()
    control = (f'ControlSocket {tor_path(root / "control")}\nCookieAuthentication 1\nDisableNetwork 1\n'
               if publishing else '')
    config.write_text(f'DataDirectory {tor_path(root / "data")}\nCacheDirectory {tor_path(cache)}\nLog notice stdout\nSafeLogging 1\n__OwningControllerProcess {os.getpid()}\n' + control + extra)
    # Keep the lease in Tor as well, so a killed parent cannot prematurely expose
    # its cache slot while the owning-process monitor is still shutting Tor down.
    # Terminal SIGINT must reach the host, not kill its transport before SSH can
    # notify the guest. Explicit teardown and the owning-process monitor remain.
    proc = await asyncio.create_subprocess_exec(executable, '-f', str(config), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, pass_fds=(() if lease is None else (lease,)), start_new_session=True)
    async def bootstrap():
        recent = []
        while line := await proc.stdout.readline():
            text = line.decode(errors='replace').strip()
            recent.append(text)
            recent = recent[-8:]
            if 'Bootstrapped 100%' in text:
                return
        raise RuntimeError('Tor stopped during startup: ' + '\n'.join(recent))
    drain = None
    publication = None
    try:
        if publishing:
            # Subscribe before enabling the network: a fast cached bootstrap must
            # not race past the descriptor-upload events we need to observe.
            try:
                publication = await asyncio.wait_for(Publication.connect(root, proc), 15)
            except asyncio.TimeoutError as exc:
                raise RuntimeError('Tor publication control startup timed out.') from exc
        try:
            await asyncio.wait_for(bootstrap(), 180)
        except asyncio.TimeoutError as exc:
            raise RuntimeError('Tor startup timed out after 180 seconds.') from exc
        async def discard():
            while await proc.stdout.read(65536):
                pass
        drain = asyncio.create_task(discard())
        if publication:
            status('Publishing invitation…')
            try:
                await asyncio.wait_for(publication.wait(), 180)
            except asyncio.TimeoutError as exc:
                raise RuntimeError('Onion service publication timed out; no invitation was issued.') from exc
            publication.writer.close()
            await publication.writer.wait_closed()
            publication = None
        yield proc
    finally:
        if publication:
            publication.writer.close()
            with contextlib.suppress(OSError):
                await publication.writer.wait_closed()
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        if drain:
            await drain


class Publication:
    """Observe authenticated Tor HS_DESC acknowledgments for this service only."""
    def __init__(self, reader, writer, root):
        self.reader, self.writer, self.root = reader, writer, root
        self.uploads = set()

    def event(self, line):
        fields = line.split()
        if len(fields) >= 6 and fields[:3] == [b'650', b'HS_DESC', b'UPLOADED']:
            onion = (self.root / 'service/hostname').read_text().strip().removesuffix('.onion').encode()
            if fields[3] == onion and fields[5] != b'UNKNOWN':
                self.uploads.add(fields[5])

    async def command(self, text):
        self.writer.write(text + b'\r\n')
        await self.writer.drain()
        while line := await self.reader.readline():
            if line.startswith(b'650 '):
                self.event(line)
            elif line == b'250 OK\r\n':
                return
            else:
                raise RuntimeError('Tor publication control command failed.')
        raise RuntimeError('Tor control connection closed during publication.')

    @classmethod
    async def connect(cls, root, proc):
        while True:
            if proc.returncode is not None:
                raise RuntimeError('Tor stopped before its control socket became ready.')
            try:
                cookie = (root / 'data/control_auth_cookie').read_bytes().hex()
                reader, writer = await asyncio.open_unix_connection(str(root / 'control'))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                await asyncio.sleep(0.05)
        instance = cls(reader, writer, root)
        try:
            await instance.command(f'AUTHENTICATE {cookie}'.encode())
            await instance.command(b'SETEVENTS HS_DESC')
            await instance.command(b'SETCONF DisableNetwork=0')
            return instance
        except BaseException:
            writer.close()
            await writer.wait_closed()
            raise

    async def wait(self):
        # Wait for a quorum of HSDir uploads before issuing the invitation.
        # v3 onion services upload to hsdir_n_replicas (2) × hsdir_spread_store (4) = 8 HSDirs.
        # Clients query hsdir_spread_fetch (3) HSDirs per replica. Waiting for fewer uploads
        # creates a race where the client may query HSDirs that don't have the descriptor yet.
        while len(self.uploads) < 8:
            line = await self.reader.readline()
            if not line:
                raise RuntimeError('Tor stopped before publishing the onion service.')
            self.event(line)


async def host(args, approval=None):
    with tempfile.TemporaryDirectory(prefix='rendezvous-') as tmp:
        root = Path(tmp)
        service = root / 'service'
        authdir = service / 'authorized_clients'
        authdir.mkdir(parents=True, mode=0o700)
        key = X25519PrivateKey.generate()
        (authdir / 'guest.auth').write_text('descriptor:x25519:' + b32(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)) + '\n')
        secret = secrets.token_urlsafe(32)
        gate = Gate(secret, float('inf'))
        gate.log = HostLog(getattr(args, 'verbose', False))
        gate.approval = approval or CommandApproval(getattr(args, 'approve', False))
        hostkey = asyncssh.generate_private_key('ssh-ed25519')
        server = await asyncssh.create_server(lambda: Server(gate), '127.0.0.1', 0, server_host_keys=[hostkey], process_factory=lambda p: shell_session(p, gate), encoding=None, login_timeout=120)
        try:
            status('Starting Tor…')
            extra = f'SocksPort 0\nHiddenServiceDir {tor_path(service)}\nHiddenServicePort 22 127.0.0.1:{server.get_port()}\n'
            if args.single_hop:
                extra += 'HiddenServiceSingleHopMode 1\nHiddenServiceNonAnonymousMode 1\n'
            async with tor(root, extra) as torproc, host_ssh_lifetime(server, gate):
                onion = (service / 'hostname').read_text().strip()
                gate.deadline = time.monotonic() + args.invite_timeout
                invite = encode_invitation(dict(onion=onion, auth=b32(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())), secret=secret, fingerprint=hostkey.get_fingerprint(), expires=int(time.time() + args.invite_timeout)))
                status('Invitation:', '1')
                print(paint(invite, '1;36', sys.stdout), flush=True)
                status('Waiting for single-use connection.')
                claim = asyncio.create_task(gate.claimed.wait())
                died = asyncio.create_task(torproc.wait())
                finished = None
                try:
                    done, _ = await asyncio.wait([claim, died], timeout=args.invite_timeout, return_when=asyncio.FIRST_COMPLETED)
                    if claim not in done:
                        raise RuntimeError('Tor stopped or invitation timed out')
                    # Closing the listener blocks new TCP connections; Gate also rejects
                    # authentication on connections accepted before this close.
                    server.close()
                    # wait_closed() can wait for accepted connections on recent
                    # Python versions. Do not await it until session teardown.
                    status('Connected.', '1;32')
                    finished = asyncio.create_task(gate.finished.wait())
                    ended, _ = await asyncio.wait([finished, died], timeout=args.session_timeout, return_when=asyncio.FIRST_COMPLETED)
                    if finished in ended and not gate.owner.conn.is_closed():
                        # Let the peer receive the shell's exit status and close
                        # SSH before terminating the Tor transport underneath it.
                        try:
                            await asyncio.wait_for(gate.owner.conn.wait_closed(), 5)
                        except asyncio.TimeoutError:
                            pass
                finally:
                    for task in [claim, died, finished]:
                        if task:
                            task.cancel()
                    await asyncio.gather(*(t for t in [claim, died, finished] if t), return_exceptions=True)
        finally:
            await close_host_ssh(server, gate)
            status('Session ended.')


@contextlib.asynccontextmanager
async def host_ssh_lifetime(server, gate):
    try:
        yield
    finally:
        await close_host_ssh(server, gate)


class PinnedClient(asyncssh.SSHClient):
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint

    def validate_host_public_key(self, host, addr, port, key):
        return hmac.compare_digest(key.get_fingerprint(), self.fingerprint)


async def tor_socks_port(root):
    reader, writer = await asyncio.open_unix_connection(str(root / 'control'))
    try:
        cookie = (root / 'data' / 'control_auth_cookie').read_bytes().hex()
        writer.write(f'AUTHENTICATE {cookie}\r\n'.encode())
        await writer.drain()
        if await reader.readline() != b'250 OK\r\n':
            raise RuntimeError('Tor control authentication failed')
        writer.write(b'GETINFO net/listeners/socks\r\n')
        await writer.drain()
        reply = await reader.readline()
        match = re.fullmatch(rb'250-net/listeners/socks="127\.0\.0\.1:(\d+)"\r\n', reply)
        if not match or await reader.readline() != b'250 OK\r\n':
            raise RuntimeError('Unexpected Tor SOCKS listener')
        return int(match[1])
    finally:
        writer.close()
        await writer.wait_closed()


async def socks_connect(port, hostname):
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    try:
        writer.write(b'\x05\x01\x00')
        await writer.drain()
        if await reader.readexactly(2) != b'\x05\x00':
            raise RuntimeError('SOCKS authentication failed')
        name = hostname.encode('ascii')
        writer.write(b'\x05\x01\x00\x03' + bytes([len(name)]) + name + b'\x00\x16')
        await writer.drain()
        reply = await reader.readexactly(4)
        if reply[0] != 5 or reply[2] != 0:
            raise RuntimeError('Invalid Tor SOCKS reply.')
        if reply[1] != 0:
            raise OSError(f'Tor could not reach the onion service (SOCKS status {reply[1]}).')
        size = {1: 4, 4: 16}.get(reply[3])
        if reply[3] == 3:
            size = (await reader.readexactly(1))[0]
        if size is None:
            raise RuntimeError('Invalid SOCKS reply')
        await reader.readexactly(size + 2)
        sock = writer.get_extra_info('socket').dup()
        sock.setblocking(False)
        return sock
    finally:
        writer.close()
        await writer.wait_closed()


def invitation_text(args):
    if args.invitation_file:
        text = Path(args.invitation_file).read_text().strip()
    elif args.invitation is not None:
        text = args.invitation.strip()
    else:
        text = input(paint('Invitation: ', '1;36', sys.stdout)).strip()
    return text


@contextlib.asynccontextmanager
async def joined_connection(invitation):
    with tempfile.TemporaryDirectory(prefix='rendezvous-') as tmp:
        root = Path(tmp)
        authdir = root / 'auth'
        authdir.mkdir(mode=0o700)
        (authdir / 'host.auth_private').write_text(invitation['onion'][:-6] + ':descriptor:x25519:' + invitation['auth'] + '\n')
        # Ask Tor for its allocated port using a private authenticated control socket.
        extra = f'SocksPort auto\nControlSocket {tor_path(root / "control")}\nCookieAuthentication 1\nClientOnionAuthDir {tor_path(authdir)}\n'
        status('Connecting through Tor…')
        async with tor(root, extra) as torproc:
            try:
                port = await asyncio.wait_for(tor_socks_port(root), 10)
            except asyncio.TimeoutError as exc:
                raise RuntimeError('Tor SOCKS discovery timed out.') from exc
            deadline = time.monotonic() + min(180, max(0, invitation['expires'] - time.time()))
            conn = None
            last_error = 'Invitation expired before connecting.'
            retry_announced = False
            while conn is None and time.monotonic() < deadline:
                sock = None
                attempted_auth = False
                def password():
                    nonlocal attempted_auth
                    attempted_auth = True
                    return invitation['secret']
                try:
                    # Short per-attempt timeout (15s) with fast retries. Fail fast and try again
                    # on a different circuit rather than waiting 45s on a slow/broken route.
                    sock = await asyncio.wait_for(socks_connect(port, invitation['onion']), min(15, deadline - time.monotonic()))
                    conn = await asyncio.wait_for(asyncssh.connect(invitation['onion'], sock=sock,
                        config=None, username='rendezvous', password=password, preferred_auth=['password'],
                        client_keys=[], agent_path=None, known_hosts=(),
                        client_factory=lambda: PinnedClient(invitation['fingerprint']), encoding=None,
                        login_timeout=30, keepalive_interval=15, keepalive_count_max=3),
                        max(0.01, deadline - time.monotonic()))
                except (asyncssh.PermissionDenied, asyncssh.HostKeyNotVerifiable):
                    raise
                except (OSError, asyncio.TimeoutError, asyncssh.ConnectionLost) as exc:
                    last_error = str(exc) or 'Timed out reaching or handshaking with the onion service.'
                    if attempted_auth:
                        raise RuntimeError('Connection lost during authentication; the invitation may have been consumed. Ask the host for a new invitation.') from exc
                    if getattr(torproc, 'returncode', None) is not None:
                        raise RuntimeError('Tor stopped while connecting to the host.') from exc
                    if not retry_announced:
                        status('Waiting for the onion service; retrying…')
                        retry_announced = True
                    await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
                finally:
                    if sock is not None and conn is None:
                        sock.close()
            if conn is None:
                raise RuntimeError('Unable to connect before the connection deadline: ' + last_error)
            try:
                async with conn:
                    yield conn
            finally:
                if sock is not None:
                    sock.close()


async def join(args):
    invitation = decode_invitation(invitation_text(args))
    if getattr(args, 'agent', False):
        return await agent_module().start(invitation, getattr(args, 'session_timeout', 43200))
    async with joined_connection(invitation) as conn:
        status('Connected.', '1;32')
        with terminal_streams() as (stdin, stdout, stderr):
            size = shutil.get_terminal_size()
            async with conn.create_process(args.command, term_type=os.environ.get('TERM', 'xterm-256color'), term_size=(size.columns, size.lines), stdin=stdin, stdout=stdout, stderr=stderr) as process:
                loop = asyncio.get_running_loop()
                def resize():
                    size = shutil.get_terminal_size()
                    process.change_terminal_size(size.columns, size.lines)
                loop.add_signal_handler(signal.SIGWINCH, resize)
                try:
                    try:
                        await asyncio.wait_for(process.wait(), getattr(args, 'session_timeout', 43200))
                    except asyncio.TimeoutError as exc:
                        raise RuntimeError('Session lifetime reached.') from exc
                    return process.exit_status if process.exit_status is not None and process.exit_status >= 0 else 1
                finally:
                    loop.remove_signal_handler(signal.SIGWINCH)


def agent_module():
    # Load only the adjacent installed application module, never cwd/PYTHONPATH.
    if '_rdzv_agent' not in sys.modules:
        spec = importlib.util.spec_from_file_location('_rdzv_agent', Path(__file__).resolve().with_name('agent_session.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules['_rdzv_agent'] = module
    return sys.modules['_rdzv_agent']


def build_parser():
    parser = argparse.ArgumentParser(prog='rdzv', description='Temporary single-use shell access over SSH and Tor.')
    parser.add_argument('--version', action='version', version='Rendezvous 0.2.8')
    sub = parser.add_subparsers(dest='mode', required=True)
    h = sub.add_parser('host')
    h.add_argument('--invite-timeout', type=int, default=600, help='Seconds to redeem the invitation (default: 600)')
    h.add_argument('--session-timeout', type=int, default=43200, help='Session lifetime in seconds (default: 43200 / 12 hours)')
    h.add_argument('-S', '--single-hop', action='store_true', help='Experimental shorter host-side Tor route; gives up host-location anonymity')
    h.add_argument('--verbose', '-v', action='store_true', help='Log all outgoing terminal output instead of one-line previews')
    h.add_argument('--approve', action='store_true', help='Require local approval for each agent command (off by default)')
    j = sub.add_parser('join')
    invitations = j.add_mutually_exclusive_group()
    invitations.add_argument('--invitation', help='Invitation text (visible in shell history and process arguments)')
    invitations.add_argument('--invitation-file', help='Read invitation from a private file instead of prompting')
    mode = j.add_mutually_exclusive_group()
    mode.add_argument('--command', help='Execute one command instead of an interactive shell')
    mode.add_argument('-a', '--agent', action='store_true', help='Keep SSH in the background and print local session controls')
    j.add_argument('--session-timeout', type=int, default=43200, help='Client session lifetime in seconds (default: 43200 / 12 hours)')
    session = sub.add_parser('session', help='Control a local background agent session')
    operations = session.add_subparsers(dest='action', required=True)
    execute = operations.add_parser('exec', help='Execute a command and return its stdout, stderr, and exit status')
    execute.add_argument('session', help='Local session ID printed by join --agent')
    execute.add_argument('command', help='Command to run in a fresh remote shell')
    for name, help_ in [('help', 'Show controls for this session'), ('close', 'End the remote connection')]:
        operation = operations.add_parser(name, help=help_)
        operation.add_argument('session')
    return parser


def main():
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] == '_agent-worker':
        payload = json.loads(sys.stdin.buffer.read(65537))
        invitation = decode_invitation(encode_invitation(payload['invitation']))
        lifetime = payload.get('session_timeout', 43200)
        if type(lifetime) is not int or lifetime <= 0:
            raise ValueError('Session timeout must be positive.')
        sys.stdin.close()
        asyncio.run(agent_module().worker(sys.argv[2], invitation, joined_connection, lifetime))
        return
    parser = build_parser()
    args = parser.parse_args()
    if (args.mode in ('host', 'join') and args.session_timeout <= 0) or (args.mode == 'host' and args.invite_timeout <= 0):
        parser.error('Timeouts must be positive')
    async def run():
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            if args.mode == 'host':
                approval = CommandApproval(args.approve)
                with host_controls(approval):
                    return await host(args, approval)
            return await join(args)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
    try:
        if args.mode == 'session':
            sys.exit(agent_module().cli(args))
        sys.exit(asyncio.run(run()) or 0)
    except (KeyboardInterrupt, asyncio.CancelledError):
        if args.mode == 'session':
            sys.exit(130)
    except (OSError, ValueError, RuntimeError, asyncssh.Error, asyncio.TimeoutError) as exc:
        message = HostLog.safe((str(exc) or 'Operation timed out.').encode())
        status(f'Rendezvous: {message}', '31')
        sys.exit(1)


if __name__ == '__main__':
    main()
