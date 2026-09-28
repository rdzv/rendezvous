"""Official Rendezvous build, verification, and publication entry point."""
import argparse
import os
import signal
import subprocess
import sys

from release_config import ROOT, BINFMT_IMAGE


def run(*command, timeout=1800, env=None, publisher_credentials=False):
    print('+ ' + ' '.join(map(str, command)), flush=True)
    child_env = dict(os.environ if env is None else env)
    if not publisher_credentials:
        for name in ['CLOUDFLARE_API_KEY', 'CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_EMAIL']:
            child_env.pop(name, None)
    subprocess.run(list(map(str, command)), cwd=ROOT, check=True, timeout=timeout, env=child_env)


def python(tool, *args, timeout=3600):
    run(sys.executable, ROOT / 'tools' / tool, *args, timeout=timeout)


def build(replace):
    python('build_portable.py', *(['--replace-unpublished'] if replace else []))
    assets()


def assets():
    from build_portable import require_unpublished
    from build_release import verified_manifest
    require_unpublished()
    verified_manifest()
    python('vendor_tor_sources.py')
    python('build_release.py')


def verify(published=False):
    from build_release import installer_text, verified_manifest
    from build_cli_docs import render
    from release_config import container_cli
    manifest = verified_manifest()
    if (ROOT / 'public/install.sh').read_text() != installer_text(manifest):
        raise RuntimeError('Installer is stale; run the website build')
    if (ROOT / 'public/cli.html').read_text() != render(None, ROOT, command=container_cli('x86_64')):
        raise RuntimeError('CLI documentation is stale; run the website build')
    run('npm', 'ci')
    run('npm', 'test')
    test_python = ROOT / '.venv/bin/python'
    run(test_python, '-m', 'pytest', '-q', timeout=600, env=dict(os.environ, RUN_INSTALL_TEST='1'))
    run('semgrep', '--config', 'p/python', '--config', 'p/security-audit', '--error',
        '--metrics=off', '--exclude', '.build', '--exclude', '.venv',
        '--exclude', 'node_modules', '--exclude', 'public/releases', '.', timeout=600)
    python('verify_platforms.py', *(['--published'] if published else []), timeout=3600)
    python('verify_cross_platform.py', timeout=1800)


def deploy():
    from build_portable import is_published
    from build_release import source_archive
    if not is_published():
        # Capture final build/test instructions in a new release's source asset.
        # Website-only deployments never replace an already-published archive.
        source_archive()
    verify()
    run('npx', '--no-install', 'wrangler', 'deploy', '--config', 'wrangler.toml', timeout=600, publisher_credentials=True)
    python('check_deployment.py')
    python('check_deployment.py', 'https://rendezvous.sh')
    python('verify_platforms.py', '--published', '--install-only', timeout=1800)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['setup-emulation', 'build', 'assets', 'website', 'verify', 'deploy', 'publish'])
    parser.add_argument('--replace-unpublished', action='store_true', help='Rebuild staged, never-published runtime assets')
    args = parser.parse_args()
    if args.action == 'setup-emulation':
        run('docker', 'run', '--privileged', '--rm', BINFMT_IMAGE, '--install', 'arm64,arm')
    elif args.action == 'build':
        build(args.replace_unpublished)
    elif args.action == 'website':
        python('build_release.py', '--website-only')
    elif args.action == 'assets':
        assets()
    elif args.action == 'verify':
        verify()
    elif args.action == 'deploy':
        deploy()
    else:
        build(args.replace_unpublished)
        deploy()


if __name__ == '__main__':
    # Detached CI/agent runners may inherit ignored SIGINT. Give subprocesses
    # normal CLI signal handling, including the real-PTY Ctrl-C regression tests.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    main()
