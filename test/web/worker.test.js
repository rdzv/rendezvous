import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../../web/worker.js';

const env = { ASSETS: { fetch: async request => {
  const path = new URL(request.url).pathname;
  return new Response(path === '/install.sh' ? "mode=__DEFAULT_MODE__\nRELEASE_ORIGIN='https://rendezvous.fernando-eb7.workers.dev'" : path, { headers: { 'Content-Type': 'text/html' } });
} } };

test('marketing and shell domains have deterministic routing', async () => {
  for (const [url, expected] of [
    ['https://rendezvous.sh/', '/index.html'],
    ['https://rendezvous.sh/cli', '/cli.html'],
    ['https://host.rdzv.sh/', 'mode=host\n'],
    ['https://rendezvous.sh/join', 'mode=join\n'],
    ['https://host.rdzv.sh/install', 'mode=install\n'],
    ['https://join.rdzv.sh/', 'mode=join\n'],
    ['https://rendezvous.fernando-eb7.workers.dev/host', 'mode=host\n'],
  ]) {
    const response = await worker.fetch(new Request(url), env);
    const body = await response.text();
    assert.equal(body, expected.startsWith('mode=') ? expected + `RELEASE_ORIGIN='${new URL(url).origin}'` : expected);
    assert.equal(response.headers.get('X-Content-Type-Options'), 'nosniff');
    if (expected.startsWith('mode=')) {
      assert.equal(response.headers.get('Content-Type'), 'text/plain; charset=utf-8');
      assert.equal(response.headers.get('Cache-Control'), 'no-store');
    }
  }
});

test('reject writes and ignore query parameters', async () => {
  assert.equal((await worker.fetch(new Request("https://bad'host.example/host"), env)).status, 400);
  assert.equal((await worker.fetch(new Request('https://rdzv.sh/', { method: 'POST' }), env)).status, 405);
  assert.equal(await (await worker.fetch(new Request('https://host.rdzv.sh/?mode=evil&token=abc'), env)).text(), "mode=host\nRELEASE_ORIGIN='https://host.rdzv.sh'");
  assert.equal(await (await worker.fetch(new Request('https://host.rdzv.sh/', { method: 'HEAD' }), env)).text(), '');
});

test('canonical redirects preserve paths and never return a shell on the short apex', async () => {
  for (const [hostname, target] of [
    ['rdzv.sh', 'rendezvous.sh'],
    ['www.rendezvous.sh', 'rendezvous.sh'],
    ['www.rdzv.sh', 'rendezvous.sh'],
    ['host.rendezvous.sh', 'host.rdzv.sh'],
    ['join.rendezvous.sh', 'join.rdzv.sh']
  ]) {
    for (const path of ['/', '/docs?from=bookmark']) {
      const response = await worker.fetch(new Request(`https://${hostname}${path}`), env);
      assert.equal(response.status, 308);
      assert.equal(response.headers.get('Location'), `https://${target}${path}`);
      assert.equal(await response.text(), '');
    }
  }
});
