"""Shared release inputs. Runtime downloads are hash-pinned for each platform."""
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
BUILD = ROOT / '.build' / VERSION
RELEASE = ROOT / 'public' / 'releases' / VERSION
ORIGIN = 'https://rendezvous.fernando-eb7.workers.dev'
GITHUB_REPOSITORY = 'rdzv/rendezvous'
ALPINE_IMAGE = 'alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8'
UBUNTU_IMAGE = 'ubuntu:22.04@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02'
BINFMT_IMAGE = 'tonistiigi/binfmt@sha256:400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0'
PYTHON_BASE = 'https://github.com/astral-sh/python-build-standalone/releases/download/20260924/'
TOR_NETWORK_TIMEOUTS = (
    'Tor startup timed out after 180 seconds.',
    'Onion service publication timed out; no invitation was issued.',
    'Unable to connect before the connection deadline: Timed out reaching or handshaking with the onion service.',
)
PLATFORMS = {
    'x86_64': {'docker': 'linux/amd64', 'triple': 'x86_64-unknown-linux-gnu',
               'python_sha256': '269b2c99e4db15b242bf01832f4fea1e8f1a664f273cff519393f296e9820b41'},
    'aarch64': {'docker': 'linux/arm64', 'triple': 'aarch64-unknown-linux-gnu',
                'python_sha256': 'c8499b61252c433280f134df954464d19811527b31cb920c35fc6967c1222e35'},
    'armv7l': {'docker': 'linux/arm/v7', 'triple': 'armv7-unknown-linux-gnueabihf',
               'python_sha256': '72cb9e8053f88d0b9ab8039610afba635f8a8e158225f9316af5722c35930725'},
}
for config in PLATFORMS.values():
    config['python_url'] = PYTHON_BASE + 'cpython-3.12.14%2B20260924-' + config['triple'] + '-install_only_stripped.tar.gz'


def bundle_path(arch):
    return BUILD / arch / 'bundle'


def container_cli(arch):
    """Run the packaged launcher, including on a different build-host architecture."""
    return ['docker', 'run', '--rm', '--platform', PLATFORMS[arch]['docker'],
            '--network', 'none', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--user', '10001:10001',
            '-e', 'COLUMNS=80', '-e', 'NO_COLOR=',
            '--mount', f'type=bind,src={bundle_path(arch)},dst=/bundle,readonly',
            UBUNTU_IMAGE, '/bundle/bin/rdzv']
