'use strict';

const assert = require('node:assert/strict');
const http = require('node:http');
const vm = require('node:vm');
const test = require('node:test');

const adapter = require('../../src/crossover_electron_bridge/adapters/fractured-realms.cjs');

const JSON_HEADERS = { 'content-type': 'application/json' };

function request(pathname, payload, options = {}) {
  const body = options.rawBody === undefined ? JSON.stringify(payload) : options.rawBody;
  return adapter.handleApi({
    method: options.method || 'POST',
    pathname,
    headers: options.headers || JSON_HEADERS,
    body,
    shell: options.shell,
    env: options.env || {},
    services: options.services,
  });
}

test('exports exact service ABI and bridge keys', async () => {
  assert.equal(adapter.id, 'CROSSOVER_BROWSER_GAMES_FRACTURED_REALMS_V1');
  assert.equal(typeof adapter.bridgeScript, 'function');
  assert.equal(typeof adapter.handleApi, 'function');

  const listeners = new Map();
  const document = {
    fullscreenElement: null,
    documentElement: { requestFullscreen: async () => {} },
    addEventListener(name, callback) { listeners.set(name, callback); },
    removeEventListener(name, callback) { if (listeners.get(name) === callback) listeners.delete(name); },
  };
  const fetchCalls = [];
  const context = {
    window: {},
    document,
    fetch(pathname, options) {
      fetchCalls.push({ pathname, options });
      return Promise.resolve({ json: async () => ({ ok: true }) });
    },
    setTimeout,
    clearTimeout,
    console,
  };
  vm.runInNewContext(adapter.bridgeScript('test-token').replace(/^<script>|<\/script>$/g, ''), context);
  const api = context.window.electronAPI;
  assert.deepEqual(Object.keys(api).sort(), [
    'getFullscreen', 'onFullscreenChanged', 'openExternal', 'quitApp',
    'saveGame', 'setFullscreen', 'steamResetAchievements', 'steamUnlock', 'submitFeedback',
  ].sort());
  assert.deepEqual(JSON.parse(JSON.stringify(await api.saveGame('local-storage'))), { ok: true });
  assert.deepEqual(JSON.parse(JSON.stringify(await api.steamResetAchievements())), { ok: false, reason: 'disabled-in-build' });
  await api.steamUnlock('ACHIEVEMENT');
  assert.equal(fetchCalls[0].pathname, '/api/steam/unlock');
  assert.equal(fetchCalls[0].options.headers['X-Crossover-Token'], 'test-token');
  assert.deepEqual(JSON.parse(fetchCalls[0].options.body), { apiName: 'ACHIEVEMENT' });
  const unsubscribe = api.onFullscreenChanged(() => {});
  assert.equal(listeners.has('fullscreenchange'), true);
  unsubscribe();
  assert.equal(listeners.has('fullscreenchange'), false);
});

test('fullscreen bridge wires browser API and returns denial errors', async () => {
  let callback;
  let removed;
  const document = {
    fullscreenElement: null,
    documentElement: { requestFullscreen: async () => { throw new Error('permission denied'); } },
    addEventListener(name, fn) { if (name === 'fullscreenchange') callback = fn; },
    removeEventListener(name, fn) { removed = fn; },
  };
  const context = { window: {}, document, fetch: async () => ({ json: async () => ({}) }) };
  vm.runInNewContext(adapter.bridgeScript('token').replace(/^<script>|<\/script>$/g, ''), context);
  const api = context.window.electronAPI;
  assert.equal(await api.getFullscreen(), false);
  assert.deepEqual(JSON.parse(JSON.stringify(await api.setFullscreen(true))), { ok: false, error: 'permission denied' });
  let state;
  const off = api.onFullscreenChanged((value) => { state = value; });
  document.fullscreenElement = document.documentElement;
  callback();
  assert.equal(state, true);
  off();
  assert.equal(removed, callback);
});

test('steam unlock delegates each request and preserves native result shapes', async () => {
  const calls = [];
  const services = { steamUnlock: async (apiName) => {
    calls.push(apiName);
    if (apiName === 'NO_CLIENT') return { ok: false, reason: 'no-client' };
    if (apiName === 'ERROR') return { ok: false, error: 'native failure' };
    if (typeof apiName !== 'string') return { ok: false, reason: 'bad-name' };
    return { ok: true, activated: true };
  } };
  assert.deepEqual((await request('/api/steam/unlock', { apiName: 'GOOD' }, { services })).body, { ok: true, activated: true });
  assert.deepEqual((await request('/api/steam/unlock', { apiName: 'NO_CLIENT' }, { services })).body, { ok: false, reason: 'no-client' });
  assert.deepEqual((await request('/api/steam/unlock', { apiName: null }, { services })).body, { ok: false, reason: 'bad-name' });
  assert.deepEqual((await request('/api/steam/unlock', { apiName: 'ERROR' }, { services })).body, { ok: false, error: 'native failure' });
  assert.deepEqual(calls, ['GOOD', 'NO_CLIENT', null, 'ERROR']);
  assert.equal((await request('/api/steam/unlock', { apiName: 'GOOD' })).body.reason, 'no-client');
});

test('open external accepts HTTPS only and keeps shell privileged', async () => {
  const opened = [];
  const shell = { openExternal: async (url) => { opened.push(url); } };
  assert.deepEqual((await request('/api/open-external', { url: 'https://example.com/path' }, { shell })).body, { ok: true });
  assert.deepEqual((await request('/api/open-external', { url: 'http://example.com' }, { shell })).body, { ok: false, error: 'Only https URLs allowed.' });
  assert.deepEqual((await request('/api/open-external', { url: 'file:///etc/passwd' }, { shell })).body, { ok: false, error: 'Only https URLs allowed.' });
  assert.deepEqual(opened, ['https://example.com/path']);
  const failing = { openExternal: async () => { throw new Error('open failed'); } };
  assert.deepEqual((await request('/api/open-external', { url: 'https://example.com' }, { shell: failing })).body, { ok: false, error: 'open failed' });
});

test('feedback validates payload, truncates fields, posts to selected local webhook, and reports failures', async (t) => {
  const received = [];
  const server = http.createServer((req, res) => {
    let raw = '';
    req.setEncoding('utf8');
    req.on('data', (chunk) => { raw += chunk; });
    req.on('end', () => {
      received.push(JSON.parse(raw));
      res.writeHead(req.url === '/failure' ? 500 : 204);
      res.end();
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => server.close());
  const address = server.address();
  const base = `http://127.0.0.1:${address.port}`;
  const env = { BUG_WEBHOOK_URL: `${base}/bug`, FEEDBACK_WEBHOOK_URL: `${base}/feedback` };

  assert.deepEqual((await request('/api/feedback', { type: 'feedback', message: 'hello' }, { env })).body, { ok: true });
  assert.equal(received[0].username, 'Fractured Realms');
  assert.match(received[0].content, /^Feedback \(/);
  assert.match(received[0].content, /\nhello$/);
  assert.deepEqual((await request('/api/feedback', { type: 'bug', message: 'bug report' }, { env })).body, { ok: true });
  assert.match(received[1].content, /^Bug Report \(/);
  assert.deepEqual((await request('/api/feedback', { type: 'other', message: 'x' }, { env })).body, { ok: false, error: 'Feedback type must be bug or feedback' });
  assert.deepEqual((await request('/api/feedback', { type: 'bug', message: '   ' }, { env })).body, { ok: false, error: 'Message is required.' });
  assert.deepEqual((await request('/api/feedback', { type: 'bug', message: 'x' })).body, { ok: false, error: 'Feedback is not configured.' });
  const longContact = 'c'.repeat(200);
  const longMessage = 'm'.repeat(3000);
  assert.deepEqual((await request('/api/feedback', { type: 'bug', contact: longContact, message: longMessage }, { env })).body, { ok: true });
  assert.ok(received[2].content.length <= 1900);
  assert.ok(!received[2].content.includes('c'.repeat(121)));
  const failed = await request('/api/feedback', { type: 'feedback', message: 'x' }, { env: { FEEDBACK_WEBHOOK_URL: `${base}/failure` } });
  assert.deepEqual(failed.body, { ok: false, error: 'Server returned 500' });
});

test('quit responds before scheduling app shutdown', async () => {
  let quit = false;
  const result = await request('/api/quit', {}, { services: { quitApp: () => { quit = true; } } });
  assert.deepEqual(result, { status: 200, body: { ok: true } });
  assert.equal(quit, false);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(quit, true);
});

test('known routes enforce method, media type, and JSON object body; unknown routes return null', async () => {
  assert.equal(await request('/api/not-known', {}), null);
  assert.equal((await request('/api/quit', {}, { method: 'GET' })).status, 405);
  assert.equal((await request('/api/quit', {}, { headers: { 'content-type': 'text/plain' } })).status, 415);
  assert.equal((await request('/api/quit', [], { rawBody: '[]' })).status, 400);
  assert.equal((await request('/api/quit', {}, { rawBody: '{' })).status, 400);
});
