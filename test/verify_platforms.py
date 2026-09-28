"""Verify rootless installations and real Tor host/guest sessions on every CPU."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import subprocess
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from release_config import ROOT, VERSION, RELEASE, PLATFORMS, UBUNTU_IMAGE, TOR_NETWORK_TIMEOUTS


def verify(arch, published=False, install_only=False):
    platform = PLATFORMS[arch]['docker']
    image = f'rendezvous-rootless-test:{arch}'
    subprocess.run(['docker', 'build', '--platform', platform,
                    '--build-arg', f'BASE_IMAGE={UBUNTU_IMAGE}',
                    '-f', str(ROOT / 'test/rootless-test.Dockerfile'), '-t', image, str(ROOT / 'test')],
                   check=True, timeout=900)
    command = ['docker', 'run', '--rm', '--platform', platform, '--read-only',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--tmpfs', '/home/guest:exec,uid=10001,gid=10001,mode=700', '--tmpfs', '/tmp',
               '-e', f'RDZV_VERSION={VERSION}', '-e', f'RDZV_ARCH={arch}',
               '-e', f'LOCAL_RELEASE={0 if published else 1}',
               '-e', f'SKIP_TOR_TEST={1 if install_only else 0}',
               '--mount', f'type=bind,src={RELEASE},dst=/release,readonly',
               '--mount', f'type=bind,src={ROOT}/public/install.sh,dst=/installer,readonly',
               '--mount', f'type=bind,src={ROOT}/test,dst=/tests,readonly',
               image, 'sh', '/tests/test_rootless.sh']
    for attempt in range(1, 4):
        result = subprocess.run(command, capture_output=True, text=True, timeout=1200)
        print(f'=== {arch} ({"published" if published else "staged"}), attempt {attempt} ===\n{result.stdout}{result.stderr}', flush=True)
        if result.returncode == 0:
            return arch
        if install_only or attempt == 3 or not any(message in result.stderr for message in TOR_NETWORK_TIMEOUTS):
            result.check_returncode()
        print(f'{arch}: retrying a Tor network timeout with a fresh test session ({attempt + 1}/3)', flush=True)
        time.sleep(5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--published', action='store_true')
    parser.add_argument('--install-only', action='store_true')
    args = parser.parse_args()
    if args.install_only:
        with ThreadPoolExecutor(max_workers=len(PLATFORMS)) as workers:
            passed = list(workers.map(lambda arch: verify(arch, args.published, True), PLATFORMS))
    else:
        # Avoid a burst of cold Tor clients sharing one emulation host/egress.
        passed = [verify(arch, args.published) for arch in PLATFORMS]
    print('Verified:', ', '.join(passed))
