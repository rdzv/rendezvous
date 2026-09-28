"""Release subprocesses receive publisher credentials only for publication."""
from pathlib import Path
import sys


def test_publisher_credentials_are_scoped_to_the_deployment_subprocess(monkeypatch):
    tools = str(Path(__file__).resolve().parents[1] / 'tools')
    sys.path.insert(0, tools)
    try:
        from release import run
    finally:
        sys.path.remove(tools)
    names = ['CLOUDFLARE_API_KEY', 'CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_EMAIL']
    for name in names:
        monkeypatch.setenv(name, 'release-test-sentinel')
    github_names = ['GH_TOKEN', 'GITHUB_TOKEN', 'GITHUB_PAT']
    for name in github_names:
        monkeypatch.setenv(name, 'github-test-sentinel')
    # Exercise real child processes: neither npm/build hooks nor tests should
    # inherit the publisher's account access from the release controller.
    run(sys.executable, '-c', f'import os; assert all(name not in os.environ for name in {names + github_names!r})')
    run(sys.executable, '-c', f'import os; assert all(os.environ[name] == "release-test-sentinel" for name in {names!r}); assert all(name not in os.environ for name in {github_names!r})',
        publisher_credentials=True)
    run(sys.executable, '-c', f'import os; assert all(os.environ[name] == "github-test-sentinel" for name in {github_names!r}); assert all(name not in os.environ for name in {names!r})',
        github_credentials=True)
