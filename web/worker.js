const securityHeaders = {
  'Content-Security-Policy': "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
  'Strict-Transport-Security': 'max-age=31536000',
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'DENY',
  'Referrer-Policy': 'no-referrer',
  'Permissions-Policy': 'camera=(), microphone=(), geolocation=()'
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!['GET', 'HEAD'].includes(request.method)) {
      return new Response('Method not allowed\n', { status: 405, headers: { ...securityHeaders, Allow: 'GET, HEAD' } });
    }
    if (url.protocol !== 'https:') {
      url.protocol = 'https:';
      return Response.redirect(url.href, 308);
    }
    // The origin is embedded in a single-quoted shell literal below.
    if (!/^[a-z0-9.-]+$/i.test(url.hostname)) {
      return new Response('Invalid hostname\n', { status: 400, headers: securityHeaders });
    }
    const aliases = {
      'www.rendezvous.sh': 'rendezvous.sh',
      'www.rdzv.sh': 'rendezvous.sh',
      'rdzv.sh': 'rendezvous.sh',
      'host.rendezvous.sh': 'host.rdzv.sh',
      'join.rendezvous.sh': 'join.rdzv.sh'
    };
    if (Object.hasOwn(aliases, url.hostname)) {
      url.hostname = aliases[url.hostname];
      return new Response(null, { status: 308, headers: { ...securityHeaders, Location: url.href } });
    }
    let path = url.pathname;
    let mode = null;
    if (path === '/' && url.hostname === 'host.rdzv.sh') mode = 'host';
    if (path === '/' && url.hostname === 'join.rdzv.sh') mode = 'join';
    if (['/host', '/host.sh', '/install.sh'].includes(path)) mode = 'host';
    if (['/join', '/join.sh'].includes(path)) mode = 'join';
    if (path === '/install') mode = 'install';
    if (mode) path = '/install.sh';
    if (path === '/') path = '/index.html';
    if (path === '/docs') path = '/docs.html';
    if (path === '/cli') path = '/cli.html';
    if (path === '/security') path = '/security.html';
    // Query strings never affect installer contents or release selection.
    const assetURL = new URL(path, url.origin);
    const upstream = await env.ASSETS.fetch(new Request(assetURL, { method: mode ? 'GET' : request.method }));
    const headers = new Headers(upstream.headers);
    for (const [name, value] of Object.entries(securityHeaders)) headers.set(name, value);
    if (mode) {
      if (!upstream.ok) return new Response('Installer unavailable\n', { status: 503, headers: securityHeaders });
      headers.set('Content-Type', 'text/plain; charset=utf-8');
      headers.set('Cache-Control', 'no-store');
      headers.delete('ETag');
      headers.delete('Content-Length');
      const body = (await upstream.text())
        .replace('__DEFAULT_MODE__', mode)
        .replace("RELEASE_ORIGIN='https://rendezvous.fernando-eb7.workers.dev'", `RELEASE_ORIGIN='${url.origin}'`);
      return new Response(request.method === 'HEAD' ? null : body, { headers });
    }
    if (path.startsWith('/releases/') && upstream.ok) headers.set('Cache-Control', 'public, max-age=31536000, immutable');
    return new Response(upstream.body, { status: upstream.status, headers });
  }
};
