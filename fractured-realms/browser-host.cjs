'use strict';

const http = require('node:http');
const https = require('node:https');
const { URL } = require('node:url');

const MARKER = 'FRACTURED_REALMS_CROSSOVER_BROWSER_V1';
const HOST = '127.0.0.1';
const PORT = 48765;
const ORIGIN = `http://${HOST}:${PORT}`;
const MAX_REQUEST_BODY = 64 * 1024;
const MAX_WEBHOOK_CONTENT = 1900;

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
  '.webmanifest': 'application/manifest+json; charset=utf-8',
  '.wav': 'audio/wav',
  '.webm': 'video/webm',
  '.webp': 'image/webp',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
};

const ELECTRON_API_SCRIPT = `<script>
window.electronAPI = {
  saveGame: async function () { return { ok: true }; },
  openExternal: async function (url) {
    try {
      const response = await fetch('/api/open-external', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: url })
      });
      return await response.json();
    } catch (error) {
      return { ok: false, error: 'Unable to contact the desktop bridge' };
    }
  },
  submitFeedback: async function (payload) {
    try {
      const response = await fetch('/api/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload || {})
      });
      return await response.json();
    } catch (error) {
      return { ok: false, error: 'Unable to contact the desktop bridge' };
    }
  }
};
</script>`;

function jsonResponse(res, status, value, headOnly) {
  const body = JSON.stringify(value);
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

function injectElectronAPI(html) {
  const moduleScript = /<script\b(?=[^>]*\btype\s*=\s*["']module["'])[^>]*>/i;
  if (moduleScript.test(html)) {
    return html.replace(moduleScript, `${ELECTRON_API_SCRIPT}\n$&`);
  }
  const headEnd = /<\/head\s*>/i;
  if (headEnd.test(html)) {
    return html.replace(headEnd, `${ELECTRON_API_SCRIPT}\n$&`);
  }
  return `${ELECTRON_API_SCRIPT}\n${html}`;
}

function safeStaticPath(pathModule, distDirectory, pathname) {
  let decoded;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    return null;
  }
  if (decoded.includes('\0')) return null;

  // Treat either slash as a separator before normalizing. This prevents an
  // encoded Windows-style traversal from becoming a filesystem escape.
  const relative = decoded.replace(/^[\\/]+/, '').replace(/[\\/]+/g, pathModule.sep);
  const normalized = pathModule.normalize(relative);
  if (normalized === '..' || normalized.startsWith(`..${pathModule.sep}`) || pathModule.isAbsolute(normalized)) {
    return null;
  }

  const root = pathModule.resolve(distDirectory);
  const candidate = pathModule.resolve(root, normalized || 'index.html');
  if (candidate !== root && !candidate.startsWith(`${root}${pathModule.sep}`)) return null;
  return candidate;
}

function readRequestBody(req) {
  const declaredLength = Number(req.headers && req.headers['content-length']);
  if (Number.isFinite(declaredLength) && declaredLength > MAX_REQUEST_BODY) {
    const error = new Error('Request body is too large');
    error.code = 'PAYLOAD_TOO_LARGE';
    req.resume();
    return Promise.reject(error);
  }

  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let settled = false;
    const fail = (error) => {
      if (settled) return;
      settled = true;
      reject(error);
    };
    req.on('data', (chunk) => {
      if (settled) return;
      size += chunk.length;
      if (size > MAX_REQUEST_BODY) {
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
      settled = true;
      resolve(Buffer.concat(chunks, size).toString('utf8'));
    });
    req.on('error', fail);
    req.on('aborted', () => fail(new Error('Request was aborted')));
  });
}

async function readJsonBody(req) {
  const raw = await readRequestBody(req);
  try {
    const value = JSON.parse(raw);
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new Error('JSON body must be an object');
    }
    return value;
  } catch {
    const error = new Error('Request body must be valid JSON');
    error.code = 'INVALID_JSON';
    throw error;
  }
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

function postDiscordWebhook(webhookURL, payload) {
  const target = validHttpsURL(webhookURL);
  if (!target) {
    const error = new Error('Feedback webhook is not configured');
    error.code = 'WEBHOOK_NOT_CONFIGURED';
    return Promise.reject(error);
  }

  const body = JSON.stringify(payload);
  return new Promise((resolve, reject) => {
    const request = https.request(target, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Length': Buffer.byteLength(body),
        'Content-Type': 'application/json',
      },
    }, (response) => {
      let responseSize = 0;
      response.on('data', (chunk) => {
        responseSize += chunk.length;
        if (responseSize > MAX_REQUEST_BODY) response.resume();
      });
      response.on('error', reject);
      response.on('end', () => {
        if (response.statusCode >= 200 && response.statusCode < 300) {
          resolve();
        } else {
          const error = new Error(`Discord webhook returned HTTP ${response.statusCode || 0}`);
          error.code = 'WEBHOOK_FAILED';
          reject(error);
        }
      });
    });
    request.setTimeout(10000, () => request.destroy(new Error('Discord webhook timed out')));
    request.on('error', reject);
    request.end(body);
  });
}

async function handleOpenExternal(req, res, shell) {
  let body;
  try {
    body = await readJsonBody(req);
  } catch (error) {
    const status = error.code === 'PAYLOAD_TOO_LARGE' ? 413 : 400;
    jsonResponse(res, status, { ok: false, error: error.message });
    return;
  }

  const target = validHttpsURL(body.url);
  if (!target) {
    jsonResponse(res, 400, { ok: false, error: 'Only HTTPS external URLs are allowed' });
    return;
  }
  try {
    await shell.openExternal(target.toString());
    jsonResponse(res, 200, { ok: true });
  } catch {
    jsonResponse(res, 502, { ok: false, error: 'Unable to open external URL' });
  }
}

async function handleFeedback(req, res) {
  let body;
  try {
    body = await readJsonBody(req);
  } catch (error) {
    const status = error.code === 'PAYLOAD_TOO_LARGE' ? 413 : 400;
    jsonResponse(res, status, { ok: false, error: error.message });
    return;
  }

  const type = body.type;
  const message = typeof body.message === 'string' ? body.message.trim() : '';
  if (type !== 'bug' && type !== 'feedback') {
    jsonResponse(res, 400, { ok: false, error: 'Feedback type must be bug or feedback' });
    return;
  }
  if (!message) {
    jsonResponse(res, 400, { ok: false, error: 'Feedback message must not be empty' });
    return;
  }

  const webhookURL = type === 'bug' ? process.env.BUG_WEBHOOK_URL : process.env.FEEDBACK_WEBHOOK_URL;
  const details = [type === 'bug' ? '**Bug Report**' : '**Feedback**', new Date().toISOString()];
  const contact = typeof body.contact === 'string' ? body.contact.trim().slice(0, 120) : '';
  if (contact) details.push(`From: \`${contact}\``);
  details.push(message.slice(0, 1800));
  if (body.context && typeof body.context === 'object' && !Array.isArray(body.context)) {
    const contextLines = Object.entries(body.context).map(([key, value]) => `${key}: ${String(value)}`);
    if (contextLines.length) details.push(['', '```', contextLines.join('\n'), '```'].join('\n'));
  }
  const content = details.join('\n').slice(0, MAX_WEBHOOK_CONTENT);

  try {
    await postDiscordWebhook(webhookURL, {
      content,
      username: type === 'bug' ? 'Fractured Realms Bug Report' : 'Fractured Realms Feedback',
    });
    jsonResponse(res, 200, { ok: true });
  } catch (error) {
    const publicError = error.code === 'WEBHOOK_NOT_CONFIGURED'
      ? 'Feedback is not configured'
      : 'Unable to send feedback';
    jsonResponse(res, 502, { ok: false, error: publicError });
  }
}

function healthPayload() {
  return { ok: true, service: MARKER, host: HOST, port: PORT };
}

function checkExistingServer() {
  return new Promise((resolve) => {
    const request = http.get(`${ORIGIN}/health`, (response) => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => {
        body += chunk;
        if (body.length > 16 * 1024) response.resume();
      });
      response.on('end', () => {
        try {
          const value = JSON.parse(body);
          resolve(response.statusCode === 200 && value.ok === true && value.service === MARKER && value.host === HOST && value.port === PORT);
        } catch {
          resolve(false);
        }
      });
    });
    request.setTimeout(1500, () => request.destroy());
    request.on('error', () => resolve(false));
  });
}

async function start({ app, shell, path, fs }) {
  if (!app || !shell || !path || !fs) throw new TypeError('start requires app, shell, path, and fs');

  const distDirectory = path.join(__dirname, '..', 'dist');
  const server = http.createServer((req, res) => {
    void (async () => {
      const headOnly = req.method === 'HEAD';
      let requestURL;
      try {
        requestURL = new URL(req.url || '/', ORIGIN);
      } catch {
        textResponse(res, 400, 'Bad Request: malformed URL', headOnly);
        return;
      }

      if (requestURL.pathname === '/health') {
        if (req.method !== 'GET' && req.method !== 'HEAD') {
          res.setHeader('Allow', 'GET, HEAD');
          textResponse(res, 405, 'Method Not Allowed', false);
          return;
        }
        jsonResponse(res, 200, healthPayload(), headOnly);
        return;
      }

      if (requestURL.pathname === '/api/open-external') {
        if (req.method !== 'POST') {
          res.setHeader('Allow', 'POST');
          textResponse(res, 405, 'Method Not Allowed', headOnly);
          return;
        }
        await handleOpenExternal(req, res, shell);
        return;
      }
      if (requestURL.pathname === '/api/feedback') {
        if (req.method !== 'POST') {
          res.setHeader('Allow', 'POST');
          textResponse(res, 405, 'Method Not Allowed', headOnly);
          return;
        }
        await handleFeedback(req, res);
        return;
      }

      if (req.method !== 'GET' && req.method !== 'HEAD') {
        res.setHeader('Allow', 'GET, HEAD');
        textResponse(res, 405, 'Method Not Allowed', headOnly);
        return;
      }

      let filePath = safeStaticPath(path, distDirectory, requestURL.pathname);
      if (!filePath) {
        textResponse(res, 400, 'Bad Request: unsafe path', headOnly);
        return;
      }

      let stats;
      try {
        const realDistDirectory = fs.realpathSync(path.resolve(distDirectory));
        const isInsideDist = (candidate) => candidate === realDistDirectory || candidate.startsWith(`${realDistDirectory}${path.sep}`);
        if (!isInsideDist(fs.realpathSync(filePath))) {
          const error = new Error('Unsafe asset path');
          error.code = 'UNSAFE_PATH';
          throw error;
        }
        stats = fs.statSync(filePath);
        if (stats.isDirectory()) {
          filePath = path.join(filePath, 'index.html');
          if (!isInsideDist(fs.realpathSync(filePath))) {
            const error = new Error('Unsafe asset path');
            error.code = 'UNSAFE_PATH';
            throw error;
          }
          stats = fs.statSync(filePath);
        }
        if (!stats.isFile()) throw new Error('Not a regular file');
      } catch (error) {
        if (error && error.code === 'UNSAFE_PATH') {
          textResponse(res, 400, 'Bad Request: unsafe path', headOnly);
        } else if (error && (error.code === 'ENOENT' || error.code === 'ENOTDIR')) {
          textResponse(res, 404, `Not Found: ${requestURL.pathname}`, headOnly);
        } else {
          textResponse(res, 500, 'Internal Server Error: unable to read packaged asset', headOnly);
        }
        return;
      }

      try {
        const extension = path.extname(filePath).toLowerCase();
        let body = fs.readFileSync(filePath);
        if (path.basename(filePath).toLowerCase() === 'index.html') {
          body = Buffer.from(injectElectronAPI(body.toString('utf8')));
        }
        const headers = {
          'Cache-Control': extension === '.html' ? 'no-store' : 'public, max-age=3600',
          'Content-Length': body.length,
          'Content-Type': MIME_TYPES[extension] || 'application/octet-stream',
        };
        if (stats.mtime) headers['Last-Modified'] = stats.mtime.toUTCString();
        res.writeHead(200, headers);
        res.end(headOnly ? undefined : body);
      } catch {
        textResponse(res, 500, 'Internal Server Error: unable to serve packaged asset', headOnly);
      }
    })().catch(() => {
      if (!res.headersSent) textResponse(res, 500, 'Internal Server Error', req.method === 'HEAD');
      else res.destroy();
    });
  });

  const closeServer = () => {
    if (server.listening) server.close();
  };
  app.on('before-quit', closeServer);

  return new Promise((resolve, reject) => {
    let settled = false;
    server.once('error', async (error) => {
      if (settled) return;
      settled = true;
      if (error && error.code === 'EADDRINUSE') {
        if (await checkExistingServer()) {
          try {
            await shell.openExternal(ORIGIN + '/');
          } catch {
            // The existing host is still valid even if the browser could not open.
          }
          if (typeof app.quit === 'function') app.quit();
          resolve({ existing: true });
        } else {
          reject(new Error(`Port ${PORT} is already in use by another process`));
        }
        return;
      }
      reject(error);
    });
    server.listen(PORT, HOST, async () => {
      if (settled) return;
      settled = true;
      try {
        await shell.openExternal(ORIGIN + '/');
      } catch {
        // Keep the server alive if the browser declined the open request.
      }
      resolve({ server, url: ORIGIN + '/' });
    });
  });
}

module.exports = { start };
