from pathlib import Path

import pytest

from tools.build_readme import render

ROOT = Path(__file__).resolve().parents[1]


def test_readme_is_generated_from_current_website_documentation():
    assert (ROOT / 'README.md').read_text() == render((ROOT / 'public/docs.html').read_text())


def test_documentation_conversion_preserves_commands_and_resolves_site_links():
    page = '''<header>Not documentation</header><main>
    <h1>Guide</h1><p><strong>Run this.</strong> See <a href="/cli">CLI</a>.</p>
    <pre>curl 'https://host.rdzv.sh?a=1&amp;b=2'


printf '&lt;invitation&gt;'
</pre><ul><li><code>--approve</code> is optional.</li><li>Keep it running.</li></ul>
    </main><footer>Not documentation either</footer>'''
    output = render(page)
    assert '## Guide' in output
    assert '**Run this.** See [CLI](https://rendezvous.sh/cli).' in output
    assert "curl 'https://host.rdzv.sh?a=1&b=2'\n\n\nprintf '<invitation>'" in output
    assert '- `--approve` is optional.\n- Keep it running.' in output
    assert 'Not documentation' not in output


def test_unknown_documentation_markup_fails_instead_of_silently_losing_content():
    with pytest.raises(ValueError, match='Unsupported documentation element'):
        render('<main><table><tr><td>Content</td></tr></table></main>')


def test_website_pages_link_to_github_and_readme_links_to_website():
    for name in ['index.html', 'docs.html', 'cli.html', 'security.html']:
        assert 'href="https://github.com/rdzv/rendezvous"' in (ROOT / 'public' / name).read_text()
    assert '[Website](https://rendezvous.sh)' in (ROOT / 'README.md').read_text()
