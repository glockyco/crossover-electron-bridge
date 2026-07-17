'use strict';

const SERVICE_ID = 'CROSSOVER_BROWSER_GAMES_FRACTURED_REALMS_V2';
const MAX_MESSAGE_LENGTH = 1800;
const MAX_CONTACT_LENGTH = 120;
const MAX_WEBHOOK_CONTENT = 1900;
const JSON_MEDIA_TYPE = 'application/json';

function response(status, body) {
  return { status, body };
}

function header(headers, name) {
  if (!headers || typeof headers !== 'object') return undefined;
  if (typeof headers.get === 'function') return headers.get(name);
  const wanted = name.toLowerCase();
  for (const [key, value] of Object.entries(headers)) {
    if (key.toLowerCase() === wanted) return value;
  }
  return undefined;
}

function hasJsonMediaType(headers) {
  const value = header(headers, 'content-type');
  if (typeof value !== 'string') return false;
  return value.split(';', 1)[0].trim().toLowerCase() === JSON_MEDIA_TYPE;
}

function parseObject(body) {
  let value;
  try {
    const raw = Buffer.isBuffer(body) ? body.toString('utf8') : String(body ?? '');
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
}

function validHttpsURL(value) {
  if (typeof value !== 'string' || !value.trim()) return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'https:' ? parsed : null;
  } catch {
    return null;
  }
}

function apiCall(token, pathname, payload) {
  return fetch(pathname, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Crossover-Token': token,
    },
    body: JSON.stringify(payload),
  }).then(async (result) => {
    try {
      return await result.json();
    } catch {
      return { ok: false, error: 'Unable to contact the desktop bridge' };
    }
  }).catch(() => ({ ok: false, error: 'Unable to contact the desktop bridge' }));
}

function bridgeScript(token) {
  const tokenLiteral = JSON.stringify(String(token));
  return `<script>\n(() => {\n  const token = ${tokenLiteral};\n  const call = (pathname, payload) => apiCall(pathname, payload);\n  const apiCall = (pathname, payload) => fetch(pathname, {\n    method: 'POST',\n    headers: { 'Content-Type': 'application/json', 'X-Crossover-Token': token },\n    body: JSON.stringify(payload)\n  }).then(async (response) => {\n    try { return await response.json(); }\n    catch { return { ok: false, error: 'Unable to contact the desktop bridge' }; }\n  }).catch(() => ({ ok: false, error: 'Unable to contact the desktop bridge' }));\n  const electronAPI = {\n    saveGame: async function () { return { ok: true }; },\n    submitFeedback: function (payload) { return call('/api/feedback', payload || {}); },\n    openExternal: function (url) { return call('/api/open-external', { url }); },\n    steamUnlock: function (apiName) { return call('/api/steam/unlock', { apiName }); },\n    steamResetAchievements: async function () { return { ok: false, reason: 'disabled-in-build' }; },\n    getFullscreen: async function () { return Boolean(document.fullscreenElement); },\n    setFullscreen: async function (enabled) {\n      try {\n        if (enabled) {\n          if (!document.documentElement || typeof document.documentElement.requestFullscreen !== 'function') {\n            throw new Error('Fullscreen API unavailable');\n          }\n          await document.documentElement.requestFullscreen();\n        } else if (typeof document.exitFullscreen === 'function' && document.fullscreenElement) {\n          await document.exitFullscreen();\n        }\n        return { ok: true };\n      } catch (error) {\n        return { ok: false, error: error && error.message ? error.message : String(error) };\n      }\n    },\n    quitApp: function () { return call('/api/quit', {}); },\n    onFullscreenChanged: function (callback) {\n      const handler = () => callback(Boolean(document.fullscreenElement));\n      document.addEventListener('fullscreenchange', handler);\n      return () => document.removeEventListener('fullscreenchange', handler);\n    }\n  };\n  window.electronAPI = electronAPI;\n})();\n</script>`;
}

function feedbackPayload(input) {
  const type = input.type;
  if (type !== 'bug' && type !== 'feedback') {
    return { error: 'Feedback type must be bug or feedback' };
  }
  const message = typeof input.message === 'string' ? input.message.trim() : '';
  if (!message) return { error: 'Message is required.' };

  const contact = typeof input.contact === 'string' ? input.contact.trim().slice(0, MAX_CONTACT_LENGTH) : '';
  const details = [`${type === 'bug' ? 'Bug Report' : 'Feedback'} (${new Date().toISOString()})`];
  if (contact) details.push(`Contact: ${contact}`);
  details.push(message.slice(0, MAX_MESSAGE_LENGTH));
  if (input.context && typeof input.context === 'object' && !Array.isArray(input.context)) {
    const contextLines = Object.entries(input.context).map(([key, value]) => `${key}: ${String(value)}`);
    if (contextLines.length) details.push(['', '```', contextLines.join('\n'), '```'].join('\n'));
  }
  return {
    type,
    webhookKey: type === 'bug' ? 'BUG_WEBHOOK_URL' : 'FEEDBACK_WEBHOOK_URL',
    payload: {
      content: details.join('\n').slice(0, MAX_WEBHOOK_CONTENT),
      username: 'Fractured Realms',
    },
  };
}

async function handleSteamUnlock(payload, services) {
  if (!services || typeof services.steamUnlock !== 'function') {
    return { ok: false, reason: 'no-client' };
  }
  try {
    const result = await services.steamUnlock(payload.apiName);
    return result;
  } catch (error) {
    return { ok: false, error: error && error.message ? error.message : String(error) };
  }
}

async function handleApi({ method, pathname, headers, body, shell, env = process.env, services } = {}) {
  const known = pathname === '/api/steam/unlock'
    || pathname === '/api/open-external'
    || pathname === '/api/feedback'
    || pathname === '/api/quit';
  if (!known) return null;
  if (method !== 'POST') return response(405, { ok: false, error: 'Method Not Allowed' });
  if (!hasJsonMediaType(headers)) return response(415, { ok: false, error: 'Content-Type must be application/json' });

  const payload = parseObject(body);
  if (!payload) return response(400, { ok: false, error: 'Request body must be valid JSON' });

  if (pathname === '/api/steam/unlock') {
    return response(200, await handleSteamUnlock(payload, services));
  }

  if (pathname === '/api/open-external') {
    const target = validHttpsURL(payload.url);
    if (!target) return response(400, { ok: false, error: 'Only https URLs allowed.' });
    try {
      if (!shell || typeof shell.openExternal !== 'function') throw new Error('Failed to open URL.');
      await shell.openExternal(target.toString());
      return response(200, { ok: true });
    } catch (error) {
      return response(502, { ok: false, error: error && error.message ? error.message : 'Failed to open URL.' });
    }
  }

  if (pathname === '/api/feedback') {
    const checked = feedbackPayload(payload);
    if (checked.error) return response(400, { ok: false, error: checked.error });
    const webhook = env && env[checked.webhookKey];
    if (typeof webhook !== 'string' || !webhook.trim()) {
      return response(502, { ok: false, error: 'Feedback is not configured.' });
    }
    try {
      const result = await fetch(webhook, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(checked.payload),
      });
      if (!result || !result.ok) {
        return response(502, { ok: false, error: `Server returned ${result && result.status ? result.status : 0}` });
      }
      return response(200, { ok: true });
    } catch (error) {
      return response(502, { ok: false, error: error && error.message ? error.message : 'Network error' });
    }
  }

  setImmediate(() => {
    try {
      if (services && typeof services.quitApp === 'function') services.quitApp();
    } catch {
      // The response has already been returned; app shutdown errors are contained by the host.
    }
  });
  return response(200, { ok: true });
}

module.exports = { id: SERVICE_ID, bridgeScript, handleApi };
