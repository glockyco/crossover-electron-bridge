'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { EventEmitter } = require('node:events');

const { start } = require('../../src/crossover_electron_bridge/runtime/electron-host.cjs');

async function freePort() {
  const server = http.createServer();
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  await new Promise((resolve) => server.close(resolve));
  return port;
}

async function request(url, options = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request(url, options, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString('utf8') }));
    });
    req.on('error', reject);
    if (options.body) req.end(options.body); else req.end();
  });
}

async function fixture() {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'electron-host-'));
  const assets = path.join(root, 'assets');
  await fsp.mkdir(assets, { recursive: true });
  await fsp.writeFile(path.join(assets, 'index.html'), '<html><head></head><body><script type="module" src="/main.js"></script></body></html>');
  await fsp.writeFile(path.join(assets, 'main.js'), 'export default 1;');
  const port = await freePort();
  const profile = {
    schema_version: 1,
    id: 'test-profile',
    display_name: 'Test Profile',
    service: 'TEST_SERVICE_V1',
    assets_relative_to_runtime: path.relative(path.join(__dirname, '../../src/crossover_electron_bridge/runtime'), assets),
    bind_host: '127.0.0.1',
    browser_host: '127.0.0.1',
    port,
    max_request_bytes: 64,
  };
  const app = new EventEmitter();
  const opened = [];
  const shell = { openExternal: async (url) => { opened.push(url); } };
  const calls = [];
  const services = { marker: 'service-secret' };
  const adapter = {
    id: profile.service,
    bridgeScript: (token) => `<script>window.testBridge = ${JSON.stringify(token)};</script>`,
    handleApi: async (requestData) => {
      calls.push(requestData);
      if (requestData.pathname === '/api/echo') return { status: 200, body: { ok: true, body: requestData.body.toString(), service: requestData.services.marker, envType: typeof requestData.env } };
      if (requestData.pathname === '/api/error') throw new Error('secret adapter error');
      return null;
    },
  };
  const handle = await start({ app, shell, path, fs, profile, adapter, services });
  return { root, assets, profile, app, opened, calls, handle, shell, adapter, services };
}

async function cleanup(fixtureData) {
  if (fixtureData.handle && fixtureData.handle.server && fixtureData.handle.server.listening) await fixtureData.handle.close();
  await fsp.rm(fixtureData.root, { recursive: true, force: true });
}

function apiHeaders(handle, profile, extra = {}) {
  return {
    Host: `${profile.browser_host}:${profile.port}`,
    Origin: `http://${profile.browser_host}:${profile.port}`,
    'X-Crossover-Token': handle.token,
    ...extra,
  };
}

test('serves assets with fresh token bridge before first module script', async () => {
  const data = await fixture();
  try {
    assert.match(data.handle.token, /^[A-Za-z0-9_-]{43}$/);
    assert.equal(data.opened[0], data.handle.url);
    const response = await request(data.handle.url);
    assert.equal(response.status, 200);
    const bridge = response.body.indexOf('window.testBridge');
    const module = response.body.indexOf('<script type="module"');
    assert.ok(bridge >= 0 && bridge < module);
    assert.ok(!response.body.includes('service-secret'));
    assert.ok(!response.body.includes('BUG_WEBHOOK_URL'));
    const head = await request(`${data.handle.url}main.js`, { method: 'HEAD' });
    assert.equal(head.status, 200);
    assert.equal(head.body, '');
    assert.equal(head.headers['content-type'], 'application/javascript; charset=utf-8');
  } finally { await cleanup(data); }
});

test('enforces exact Host, Origin, and constant-time token authentication', async () => {
  const data = await fixture();
  try {
    const base = `${data.handle.url}api/echo`;
    const noToken = await request(base, { method: 'POST', headers: { Host: `${data.profile.browser_host}:${data.profile.port}` } });
    assert.equal(noToken.status, 401);
    const wrongHost = await request(base, { method: 'POST', headers: apiHeaders(data.handle, data.profile, { Host: `localhost:${data.profile.port}` }) });
    assert.equal(wrongHost.status, 403);
    const wrongOrigin = await request(base, { method: 'POST', headers: apiHeaders(data.handle, data.profile, { Origin: 'http://evil.test' }) });
    assert.equal(wrongOrigin.status, 403);
    const absentOrigin = await request(base, { method: 'POST', headers: { Host: `${data.profile.browser_host}:${data.profile.port}`, 'X-Crossover-Token': data.handle.token }, body: 'ok' });
    assert.equal(absentOrigin.status, 200);
    assert.deepEqual(JSON.parse(absentOrigin.body), { ok: true, body: 'ok', service: 'service-secret', envType: 'object' });
  } finally { await cleanup(data); }
});

test('rejects traversal and symlinks escaping the asset root', async () => {
  const data = await fixture();
  try {
    await fsp.writeFile(path.join(data.root, 'outside.txt'), 'outside');
    await fsp.symlink(path.join(data.root, 'outside.txt'), path.join(data.assets, 'escape.txt'));
    const traversal = await request(data.handle.url, { path: '/%2f..%2foutside.txt' });
    assert.equal(traversal.status, 400);
    const symlink = await request(`${data.handle.url}escape.txt`);
    assert.equal(symlink.status, 400);
    const missing = await request(`${data.handle.url}missing.js`);
    assert.equal(missing.status, 404);
  } finally { await cleanup(data); }
});

test('health is public and method handling is explicit', async () => {
  const data = await fixture();
  try {
    const health = await request(`${data.handle.url}health`);
    assert.equal(health.status, 200);
    assert.deepEqual(JSON.parse(health.body), { ok: true, service: data.profile.service, host: data.profile.bind_host, port: data.profile.port });
    const head = await request(`${data.handle.url}health`, { method: 'HEAD' });
    assert.equal(head.status, 200);
    assert.equal(head.body, '');
    const post = await request(`${data.handle.url}health`, { method: 'POST' });
    assert.equal(post.status, 405);
    const staticPost = await request(`${data.handle.url}main.js`, { method: 'POST' });
    assert.equal(staticPost.status, 405);
  } finally { await cleanup(data); }
});

test('bounds API bodies and contains adapter errors', async () => {
  const data = await fixture();
  try {
    const headers = apiHeaders(data.handle, data.profile);
    const tooLarge = await request(`${data.handle.url}api/echo`, { method: 'POST', headers: { ...headers, 'Content-Length': '65' }, body: 'x'.repeat(65) });
    assert.equal(tooLarge.status, 413);
    const badTransfer = await request(`${data.handle.url}api/echo`, { method: 'POST', headers: { ...headers, 'Transfer-Encoding': 'chunked' }, body: 'x' });
    assert.equal(badTransfer.status, 400);
    const adapterError = await request(`${data.handle.url}api/error`, { method: 'POST', headers });
    assert.equal(adapterError.status, 500);
    assert.deepEqual(JSON.parse(adapterError.body), { ok: false, error: 'Internal Server Error' });
    const unknown = await request(`${data.handle.url}api/unknown`, { method: 'POST', headers });
    assert.equal(unknown.status, 404);
  } finally { await cleanup(data); }
});

test('detects same-profile and rejects a different listener', async () => {
  const first = await fixture();
  const secondRoot = await fsp.mkdtemp(path.join(os.tmpdir(), 'electron-host-second-'));
  const secondApp = new EventEmitter();
  const secondOpened = [];
  const secondShell = { openExternal: async (url) => secondOpened.push(url) };
  const secondAdapter = { id: first.profile.service, bridgeScript: () => '<script></script>', handleApi: async () => null };
  try {
    const duplicate = await start({ app: secondApp, shell: secondShell, path, fs, profile: first.profile, adapter: secondAdapter, services: {} });
    assert.equal(duplicate.existing, true);
    assert.equal(duplicate.url, first.handle.url);
    assert.equal(secondOpened[0], first.handle.url);
    assert.equal(secondApp.listenerCount('before-quit'), 1);
  } catch (error) {
    assert.fail(error);
  } finally {
    await cleanup(first);
    await fsp.rm(secondRoot, { recursive: true, force: true });
  }

  const occupied = http.createServer((req, res) => { res.writeHead(200); res.end('other'); });
  await new Promise((resolve) => occupied.listen(first.profile.port, first.profile.bind_host, resolve));
  const app = new EventEmitter();
  await assert.rejects(() => start({ app, shell: secondShell, path, fs, profile: first.profile, adapter: secondAdapter, services: {} }), new RegExp(`Port ${first.profile.port} is already in use`));
  await new Promise((resolve) => occupied.close(resolve));
});

test('before-quit closes the server', async () => {
  const data = await fixture();
  data.app.emit('before-quit');
  await new Promise((resolve) => setImmediate(resolve));
  await assert.rejects(() => request(data.handle.url), /ECONNREFUSED/);
  await cleanup(data);
});

test('validates adapter ABI and identity', async () => {
  const data = await fixture();
  await data.handle.close();
  await fsp.rm(data.root, { recursive: true, force: true });
  await assert.rejects(() => start({ app: new EventEmitter(), shell: data.shell, path, fs, profile: data.profile, adapter: { id: 'wrong', bridgeScript() {}, handleApi() {} } }), /does not match profile service/);
  await assert.rejects(() => start({ app: new EventEmitter(), shell: data.shell, path, fs, profile: data.profile, adapter: { id: data.profile.service } }), /adapter ABI/);
});
