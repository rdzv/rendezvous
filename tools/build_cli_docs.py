"""Generate the public command reference from the actual CLI's help output."""
import html
import os
import subprocess


def render(python, root, *, command=None):
    environment = {name: value for name, value in os.environ.items()
                   if name not in {'CLOUDFLARE_API_KEY', 'CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_EMAIL'}}
    environment.update(COLUMNS='80', NO_COLOR='')
    def cli(*args):
        invocation = command if command is not None else [str(python), '-I', str(root / 'rendezvous.py')]
        return subprocess.run([*invocation, *args],
            check=True, capture_output=True, text=True, timeout=120,
            env=environment).stdout.strip()
    version = html.escape(cli('--version'))
    release_version = version.removeprefix('Rendezvous ')
    sections = []
    for mode, title in [('', 'General options'), ('host', 'Host options'), ('join', 'Join options'),
                        ('session', 'Agent session controls'), ('session exec', 'Execute a command'),
                        ('session help', 'Session instructions'), ('session close', 'Close session')]:
        args = [*mode.split(), '--help']
        sections.append(f'<h2>{title}</h2><pre>{html.escape(cli(*args))}</pre>')
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CLI reference — Rendezvous</title><link rel="icon" href="/mark.svg"><link rel="stylesheet" href="/style.css"></head><body>
<header class="nav wrap"><a class="brand" href="/"><img src="/mark.svg" alt="" width="32" height="32">rendezvous<span>.sh</span></a><nav aria-label="Main navigation"><a href="/">Home</a><a href="/docs">Docs</a><a href="/cli">CLI</a><a href="/security">Security</a><a class="nav-cta" href="/#start">Get a shell <span aria-hidden="true">↗</span></a></nav></header>
<main class="doc wrap"><p class="eyebrow">''' + version + '''</p><h1>CLI reference.</h1><p>Generated from this release's actual <code>--help</code> output. Options are refreshed automatically with every release build.</p>
<h2>Using options with the website commands</h2><pre>curl -fsSL https://host.rdzv.sh | sh -s -- --verbose
curl -fsSL https://host.rdzv.sh | sh -s -- --approve
curl -fsSL https://join.rdzv.sh | sh -s -- --invitation 'rv1.…'
curl -fsSL https://join.rdzv.sh | sh -s -- -a --invitation "&lt;invitation&gt;"
curl -fsSL https://host.rdzv.sh | sh -s -- --help
curl -fsSL https://join.rdzv.sh | sh -s -- --help</pre>
''' + '\n'.join(sections) + '''
<h2>Agent mode</h2><p><code>-a</code> / <code>--agent</code> starts a detached joiner and prints session-specific commands with an absolute helper path. <code>session exec SESSION COMMAND</code> streams the command's stdout and stderr and returns its exit code in one call. Each command uses a fresh non-interactive shell; use <code>cd /path &amp;&amp; ...</code> when needed. Canceling a local exec call stops that command. Close explicitly when finished. Local controls use a private same-user Unix socket.</p>
<h2>Session lifetime</h2><p>Host and client default to twelve hours (43,200 seconds). <code>--session-timeout SECONDS</code> adjusts either side; the earlier deadline ends the session. The background agent's limit includes startup and approval waiting. The controller exits when the connection ends.</p>
<h2>Command approval and host controls</h2><p><code>host --approve</code> requires local approval of agent exec requests. It is off by default and requires a controlling terminal. Interactive human sessions use a host without this option. The full command is logged, followed by <code>Approve command?</code>; repeated unbound keys change it to <code>Approve command (a/A/d)?</code>.</p><pre>a: approve, A: approve all, d: deny, D: disconnect</pre><p>Those bindings appear in the bottom bar only while a command awaits a decision. Only the a/A keys are green and d/D keys red; punctuation and descriptions stay gray. A approves the current request and all subsequent requests. Denial returns exit status 126 without executing the command. Otherwise, the bar shows only <code>D: disconnect</code>. Ctrl-C remains supported. The bar follows resizing and is removed on exit; redirected logs contain no bar control sequences.</p>
<h2>Host logging</h2><p><code>[input]</code> shows complete agent exec requests and interactive human input. Human keystrokes append to the same line until Enter; they are not a reconstruction of commands after shell editing or expansion. Incoming content is red, output cyan, and labels stay neutral.</p><p><code>[output]</code> follows logical newline boundaries, independent of SSH packet sizes. Long host log lines are shortened by default; <code>--verbose</code> includes full host output logs. Human PTYs combine stdout/stderr; agent exec keeps them separate. Guest command output is unchanged. Host logs are written to standard error.</p>
</main><footer class="wrap"><a class="brand" href="/">rendezvous<span>.sh</span></a><div><a href="/docs">Documentation</a><a href="/cli">CLI reference</a><a href="/security">Security</a><a href="/releases/''' + release_version + '''/rendezvous-''' + release_version + '''-source.tar.gz">Source</a></div></footer></body></html>
'''


if __name__ == '__main__':
    from release_config import ROOT, container_cli
    (ROOT / 'public/cli.html').write_text(render(None, ROOT, command=container_cli('x86_64')))
