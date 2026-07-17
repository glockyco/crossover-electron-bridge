'use strict';

const http = require('node:http');
const crypto = require('node:crypto');
const { URL } = require('node:url');

const DEFAULT_MAX_REQUEST_BYTES = 64 * 1024;
const MIME_TYPES = {
  '.avif': 'image/avif',
  '.css': 'text/css; charset=utf-8',
  '.gif': 'image/gif',
  '.html': 'text/html; charset=utf-8',
  '.ico': 'image/x-icon',
  '.jpeg': 'image/jpeg',
  '.jpg': 'image/jpeg',
  '.js': 'application/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
  '.mjs': 'application/javascript; charset=utf-8',
  '.mp3': 'audio/mpeg',
  '.mp4': 'video/mp4',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.txt': 'text/plain; charset=utf-8',
  '.wasm': 'application/wasm',
  '.wav': 'audio/wav',
  '.webm': 'video/webm',
  '.webmanifest': 'application/manifest+json; charset=utf-8',
  '.webp': 'image/webp',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
};

function jsonResponse(res, status, value, headOnly) {
  let body;
  try {
    body = JSON.stringify(value);
  } catch {
    body = JSON.stringify({ ok: false, error: 'Internal Server Error' });
    status = 500;
  }
  if (body === undefined) {
    body = JSON.stringify({ ok: false, error: 'Internal Server Error' });
    status = 500;
  }
  res.writeHead(status, {
    'Cache-Control': 'no-store',
    'Content-Length': Buffer.byteLength(body),
    'Content-Type': 'application/json; charset=utf-8',
  });
  res.end(headOnly ? undefined : body);
}

function textResponse(res, status, message, headOnly, contentType = 'text/plain; charset=utf-8') {
  const body = String(message);
  res.writeHead(status, {
    'Cache-Control': 'no-store',
    'Content-Length': Buffer.byteLength(body),
    'Content-Type': contentType,
  });
  res.end(headOnly ? undefined : body);
}

function normalizeBridgeScript(script) {
  if (/<script\b/i.test(script)) return script;
  return `<script>\n${script}\n</script>`;
}

function injectBridge(html, script) {
  const bridge = normalizeBridgeScript(script);
  const moduleScript = /<script\b(?=[^>]*\btype\s*=\s*["']module["'])[^>]*>/i;
  if (moduleScript.test(html)) return html.replace(moduleScript, `${bridge}\n$&`);
  const headEnd = /<\/head\s*>/i;
  if (headEnd.test(html)) return html.replace(headEnd, `${bridge}\n$&`);
  return `${bridge}\n${html}`;
}

function safeStaticPath(pathModule, fsModule, rootDirectory, pathname) {
  let decoded;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    return { error: 'unsafe' };
  }
  if (decoded.includes('\0')) return { error: 'unsafe' };

  const relative = decoded.replace(/^[\\/]+/, '').replace(/[\\/]+/g, pathModule.sep);
  const normalized = pathModule.normalize(relative);
  if (normalized === '..' || normalized.startsWith(`..${pathModule.sep}`) || pathModule.isAbsolute(normalized)) {
    return { error: 'unsafe' };
  }
  const root = pathModule.resolve(rootDirectory);
  let candidate = pathModule.resolve(root, normalized || 'index.html');
  if (candidate !== root && !candidate.startsWith(`${root}${pathModule.sep}`)) return { error: 'unsafe' };

  let realRoot;
  try {
    realRoot = fsModule.realpathSync(root);
  } catch (error) {
    return { error: error && (error.code === 'ENOENT' || error.code === 'ENOTDIR') ? 'missing' : 'internal' };
  }
  const inside = (value) => value === realRoot || value.startsWith(`${realRoot}${pathModule.sep}`);
  let realCandidate;
  try {
    realCandidate = fsModule.realpathSync(candidate);
    if (!inside(realCandidate)) return { error: 'unsafe' };
    let stats = fsModule.statSync(candidate);
    if (stats.isDirectory()) {
      candidate = pathModule.join(candidate, 'index.html');
      realCandidate = fsModule.realpathSync(candidate);
      if (!inside(realCandidate)) return { error: 'unsafe' };
      stats = fsModule.statSync(candidate);
    }
    if (!stats.isFile()) return { error: 'missing' };
    return { path: candidate, stats };
  } catch (error) {
    if (error && error.code === 'UNSAFE_PATH') return { error: 'unsafe' };
    if (error && (error.code === 'ENOENT' || error.code === 'ENOTDIR')) return { error: 'missing' };
    return { error: 'internal' };
  }
}

function headerValue(headers, name) {
  const value = headers && headers[name.toLowerCase()];
  return Array.isArray(value) ? null : value;
}

function tokenMatches(actual, expected) {
  if (typeof actual !== 'string') return false;
  const left = Buffer.from(actual);
  const right = Buffer.from(expected);
  return left.length === right.length && crypto.timingSafeEqual(left, right);
}

function parseDotEnv(fsModule, pathModule) {
  const values = {};
  const candidates = [
    pathModule.join(process.cwd(), '.env'),
    pathModule.join(__dirname, '.env'),
    pathModule.join(__dirname, '..', '.env'),
    pathModule.join(__dirname, '..', '..', '.env'),
    pathModule.join(__dirname, '..', '..', '..', '.env'),
  ];
  const seen = new Set();
  for (const filename of candidates) {
    if (seen.has(filename)) continue;
    seen.add(filename);
    let source;
    try {
      source = fsModule.readFileSync(filename, 'utf8');
    } catch {
      continue;
    }
    for (const line of String(source).split(/\r?\n/)) {
      const match = /^\s*(?:export\s+)?(BUG_WEBHOOK_URL|FEEDBACK_WEBHOOK_URL)\s*=\s*(.*?)\s*$/.exec(line);
      if (!match) continue;
      let value = match[2];
      if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
        value = value.slice(1, -1);
      } else {
        value = value.replace(/\s+#.*$/, '');
      }
      if (!(match[1] in values)) values[match[1]] = value;
    }
  }
  return values;
}

function frozenEnvironment(fsModule, pathModule) {
  const local = parseDotEnv(fsModule, pathModule);
  const snapshot = { ...local, ...process.env };
  return Object.freeze(snapshot);
}

function validateStartConfig(config) {
  if (!config || typeof config !== 'object') throw new TypeError('start requires a configuration object');
  const { app, shell, path: pathModule, fs: fsModule, profile, adapter } = config;
  if (!app || !shell || !pathModule || !fsModule) throw new TypeError('start requires app, shell, path, and fs');
  if (!profile || typeof profile !== 'object') throw new TypeError('start requires profile');
  const required = ['schema_version', 'id', 'display_name', 'service', 'assets_relative_to_runtime', 'bind_host', 'browser_host', 'port', 'max_request_bytes'];
  const unknown = Object.keys(profile).filter((key) => !required.includes(key));
  if (unknown.length) throw new TypeError(`Invalid browser profile: unknown ${unknown.join(', ')}`);
  const missing = required.filter((key) => !(key in profile));
  if (missing.length) throw new TypeError(`Invalid browser profile: missing ${missing.join(', ')}`);
  if (profile.schema_version !== 1 || typeof profile.id !== 'string' || !profile.id || typeof profile.display_name !== 'string' || !profile.display_name) {
    throw new TypeError('Invalid browser profile identity');
  }
  if (typeof profile.service !== 'string' || !profile.service || typeof profile.assets_relative_to_runtime !== 'string' || !profile.assets_relative_to_runtime || pathModule.isAbsolute(profile.assets_relative_to_runtime) || profile.assets_relative_to_runtime.includes('\0')) {
    throw new TypeError('Invalid browser profile paths or service');
  }
  const loopbackHosts = new Set(['127.0.0.1', 'localhost', '::1']);
  if (typeof profile.bind_host !== 'string' || !loopbackHosts.has(profile.bind_host) || typeof profile.browser_host !== 'string' || !loopbackHosts.has(profile.browser_host) || !Number.isInteger(profile.port) || profile.port < 1 || profile.port > 65535 || !Number.isInteger(profile.max_request_bytes) || profile.max_request_bytes < 1) {
    throw new TypeError('Invalid browser profile network settings');
  }
  if (!adapter || typeof adapter !== 'object' || typeof adapter.id !== 'string' || typeof adapter.bridgeScript !== 'function' || typeof adapter.handleApi !== 'function') {
    throw new TypeError('Invalid browser adapter ABI');
  }
  if (adapter.id !== profile.service) throw new TypeError('Browser adapter does not match profile service');
  return { app, shell, path: pathModule, fs: fsModule, profile, adapter };
}

function expectedOrigin(profile) {
  return `http://${profile.browser_host}:${profile.port}`;
}

function expectedHosts(profile) {
  return new Set([`${profile.bind_host}:${profile.port}`, `${profile.browser_host}:${profile.port}`]);
}

function checkExistingServer(profile, httpModule = http) {
  const origin = expectedOrigin(profile);
  const hosts = expectedHosts(profile);
  return new Promise((resolve) => {
    const request = httpModule.get(`${origin}/health`, { headers: { Host: `${profile.browser_host}:${profile.port}` } }, (response) => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => {
        body += chunk;
        if (body.length > 16 * 1024) response.resume();
      });
      response.on('end', () => {
        try {
          const value = JSON.parse(body);
          resolve(response.statusCode === 200 && value && value.ok === true && value.service === profile.service && hosts.has(`${value.host}:${value.port}`));
        } catch {
          resolve(false);
        }
      });
    });
    request.setTimeout(1500, () => request.destroy());
    request.on('error', () => resolve(false));
  });
}

function readBody(req, limit) {
  const declared = headerValue(req.headers, 'content-length');
  if (declared !== undefined && !/^\d+$/.test(String(declared))) {
    const error = new Error('Invalid Content-Length');
    error.code = 'BAD_CONTENT_LENGTH';
    req.resume();
    return Promise.reject(error);
  }
  const length = declared === undefined ? null : Number(declared);
  if (length !== null && (!Number.isSafeInteger(length) || length > limit)) {
    const error = new Error('Request body is too large');
    error.code = 'PAYLOAD_TOO_LARGE';
    req.resume();
    return Promise.reject(error);
  }
  const transfer = headerValue(req.headers, 'transfer-encoding');
  if (transfer && String(transfer).toLowerCase() !== 'identity') {
    const error = new Error('Unsupported transfer encoding');
    error.code = 'BAD_TRANSFER_ENCODING';
    req.resume();
    return Promise.reject(error);
  }
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let settled = false;
    const fail = (error) => {
      if (!settled) { settled = true; reject(error); }
    };
    req.on('data', (chunk) => {
      if (settled) return;
      size += chunk.length;
      if (size > limit) {
        const error = new Error('Request body is too large');
        error.code = 'PAYLOAD_TOO_LARGE';
        req.resume();
        fail(error);
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      if (settled) return;
      if (length !== null && size !== length) {
        const error = new Error('Incomplete request body');
        error.code = 'BAD_CONTENT_LENGTH';
        fail(error);
        return;
      }
      settled = true;
      resolve(Buffer.concat(chunks, size));
    });
    req.on('aborted', () => fail(new Error('Request was aborted')));
    req.on('error', fail);
  });
}

function errorStatus(error) {
  if (!error) return 500;
  if (error.code === 'PAYLOAD_TOO_LARGE') return 413;
  if (error.code === 'BAD_CONTENT_LENGTH' || error.code === 'BAD_TRANSFER_ENCODING') return 400;
  return 400;
}

function closePromise(server) {
  if (!server.listening) return Promise.resolve();
  return new Promise((resolve) => server.close(() => resolve()));
}

async function start(config) {
  const { app, shell, path: pathModule, fs: fsModule, profile, adapter } = validateStartConfig(config);
  const services = config.services && typeof config.services === 'object' ? Object.freeze({ ...config.services }) : Object.freeze({});
  const env = frozenEnvironment(fsModule, pathModule);
  const token = crypto.randomBytes(32).toString('base64url');
  const origin = expectedOrigin(profile);
  const assetRoot = pathModule.resolve(__dirname, profile.assets_relative_to_runtime);
  const hosts = expectedHosts(profile);
  const server = http.createServer((req, res) => {
    void (async () => {
      const headOnly = req.method === 'HEAD';
      let requestURL;
      try {
        requestURL = new URL(req.url || '/', origin + '/');
      } catch {
        textResponse(res, 400, 'Bad Request: malformed URL', headOnly);
        return;
      }
      const pathname = requestURL.pathname;
      if (pathname === '/health') {
        if (req.method !== 'GET' && req.method !== 'HEAD') {
          res.setHeader('Allow', 'GET, HEAD');
          textResponse(res, 405, 'Method Not Allowed', headOnly);
          return;
        }
        jsonResponse(res, 200, { ok: true, service: profile.service, host: profile.bind_host, port: profile.port }, headOnly);
        return;
      }

      if (pathname.startsWith('/api/')) {
        const hostHeader = headerValue(req.headers, 'host');
        const originHeader = headerValue(req.headers, 'origin');
        if (!hosts.has(hostHeader) || (originHeader !== undefined && originHeader !== origin)) {
          jsonResponse(res, 403, { ok: false, error: 'Forbidden' }, headOnly);
          return;
        }
        if (!tokenMatches(headerValue(req.headers, 'x-crossover-token'), token)) {
          jsonResponse(res, 401, { ok: false, error: 'Unauthorized' }, headOnly);
          return;
        }
        let body;
        try {
          body = await readBody(req, profile.max_request_bytes || DEFAULT_MAX_REQUEST_BYTES);
        } catch (error) {
          jsonResponse(res, errorStatus(error), { ok: false, error: error.message }, headOnly);
          return;
        }
        let result;
        try {
          result = await adapter.handleApi({
            method: req.method || '',
            pathname,
            headers: Object.freeze({ ...req.headers }),
            body,
            shell,
            env,
            services,
          });
        } catch {
          jsonResponse(res, 500, { ok: false, error: 'Internal Server Error' }, headOnly);
          return;
        }
        if (result === null) {
          jsonResponse(res, 404, { ok: false, error: 'Not Found' }, headOnly);
          return;
        }
        if (!result || !Number.isInteger(result.status) || result.status < 100 || result.status > 599 || !result.body || typeof result.body !== 'object' || Array.isArray(result.body)) {
          jsonResponse(res, 500, { ok: false, error: 'Internal Server Error' }, headOnly);
          return;
        }
        jsonResponse(res, result.status, result.body, headOnly);
        return;
      }

      if (req.method !== 'GET' && req.method !== 'HEAD') {
        res.setHeader('Allow', 'GET, HEAD');
        textResponse(res, 405, 'Method Not Allowed', headOnly);
        return;
      }
      const found = safeStaticPath(pathModule, fsModule, assetRoot, pathname);
      if (found.error === 'unsafe') {
        textResponse(res, 400, 'Bad Request: unsafe path', headOnly);
        return;
      }
      if (found.error === 'missing') {
        textResponse(res, 404, `Not Found: ${pathname}`, headOnly);
        return;
      }
      if (found.error) {
        textResponse(res, 500, 'Internal Server Error: unable to read packaged asset', headOnly);
        return;
      }
      try {
        const extension = pathModule.extname(found.path).toLowerCase();
        let body = fsModule.readFileSync(found.path);
        if (pathModule.basename(found.path).toLowerCase() === 'index.html') {
          const injected = injectBridge(body.toString('utf8'), adapter.bridgeScript(token));
          body = Buffer.from(injected);
        }
        const headers = {
          'Cache-Control': extension === '.html' ? 'no-store' : 'public, max-age=3600',
          'Content-Length': body.length,
          'Content-Type': MIME_TYPES[extension] || 'application/octet-stream',
        };
        if (found.stats.mtime) headers['Last-Modified'] = found.stats.mtime.toUTCString();
        res.writeHead(200, headers);
        res.end(headOnly ? undefined : body);
      } catch {
        if (!res.headersSent) textResponse(res, 500, 'Internal Server Error: unable to serve packaged asset', headOnly);
        else res.destroy();
      }
    })().catch(() => {
      if (!res.headersSent) textResponse(res, 500, 'Internal Server Error', req.method === 'HEAD');
      else res.destroy();
    });
  });

  let quitting = false;
  const close = async () => {
    if (quitting) return;
    quitting = true;
    if (typeof app.removeListener === 'function') app.removeListener('before-quit', onBeforeQuit);
    await closePromise(server);
  };
  const onBeforeQuit = () => { void close(); };
  if (typeof app.on === 'function') app.on('before-quit', onBeforeQuit);

  const handle = { server, url: `${origin}/`, token, close };
  return new Promise((resolve, reject) => {
    let settled = false;
    const onError = async (error) => {
      if (settled) return;
      if (error && error.code === 'EADDRINUSE') {
        if (await checkExistingServer(profile)) {
          try { await shell.openExternal(`${origin}/`); } catch { /* existing host remains valid */ }
          if (typeof app.quit === 'function') app.quit();
          settled = true;
          resolve({ ...handle, server: null, existing: true, close: async () => {} });
        } else {
          settled = true;
          if (typeof app.removeListener === 'function') app.removeListener('before-quit', onBeforeQuit);
          reject(new Error(`Port ${profile.port} is already in use by another process`));
        }
        return;
      }
      settled = true;
      if (typeof app.removeListener === 'function') app.removeListener('before-quit', onBeforeQuit);
      reject(error);
    };
    server.once('error', onError);
    server.listen(profile.port, profile.bind_host, async () => {
      if (settled) return;
      settled = true;
      try { await shell.openExternal(`${origin}/`); } catch { /* browser opening is best effort */ }
      resolve(handle);
    });
  });
}

module.exports = { start };
