import importlib.util
from pathlib import Path
import sys


def test_published_cli_reference_matches_current_parser():
    root = Path(__file__).parent
    spec = importlib.util.spec_from_file_location('build_cli_docs', root / 'tools/build_cli_docs.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (root / 'public/cli.html').read_text() == module.render(sys.executable, root)


def test_public_docs_use_canonical_endpoints_and_link_cli_reference():
    root = Path(__file__).parent / 'public'
    for name in ['index.html', 'docs.html']:
        text = (root / name).read_text()
        assert 'href="/cli"' in text
        assert 'Provider deployment' not in text
        assert 'connect.rdzv.net' not in text
        assert 'workers.dev' not in text
        assert '#preview' not in text
