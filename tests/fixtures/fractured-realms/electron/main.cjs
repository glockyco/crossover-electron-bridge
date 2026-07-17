const { app, BrowserWindow, ipcMain, shell } = require('electron');
const path = require('path');
const fs   = require('fs');

const STEAM_APP_ID = 3789070;
let steamClient = null;

function initSteam() {
  try {
    const steamworks = require('steamworks.js');
    steamClient = steamworks.init(STEAM_APP_ID);
    console.log('[steam] initialised for app', STEAM_APP_ID);
  } catch (err) {
    steamClient = null;
    console.log('[steam] not available (running outside Steam?):', err?.message ?? err);
  }
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 720,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.loadFile(path.join(__dirname, '../app/dist/index.html'));
  win.on('enter-full-screen', () => win.webContents.send('fullscreen-changed', true));
  win.on('leave-full-screen', () => win.webContents.send('fullscreen-changed', false));
}

ipcMain.handle('open-external', async (_event, url) => {
  try {
    if (typeof url !== 'string' || !url.startsWith('https://')) {
      return { ok: false, error: 'Only https URLs allowed.' };
    }
    await shell.openExternal(url);
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err?.message ?? 'Failed to open URL.' };
  }
});

ipcMain.handle('get-fullscreen', (event) => {
  const w = BrowserWindow.fromWebContents(event.sender);
  return w ? w.isFullScreen() : false;
});
ipcMain.handle('set-fullscreen', (event, val) => {
  const w = BrowserWindow.fromWebContents(event.sender);
  if (w) w.setFullScreen(!!val);
  return { ok: true };
});

ipcMain.handle('quit-app', () => {
  app.quit();
});

ipcMain.handle('save-game', (_event, data) => {
  // Placeholder: localStorage handles saves in renderer for now
  return { ok: true };
});

ipcMain.handle('submit-feedback', async (_event, payload) => {
  const isBug = payload?.type === 'bug';
  const webhook = process.env[isBug ? 'BUG_WEBHOOK_URL' : 'FEEDBACK_WEBHOOK_URL'];
  if (!webhook) return { ok: false, error: 'Feedback is not configured.' };
  const message = typeof payload?.message === 'string' ? payload.message.trim() : '';
  if (!message) return { ok: false, error: 'Message is required.' };
  const content = `${isBug ? 'Bug Report' : 'Feedback'} (${new Date().toISOString()})\n${message.slice(0, 1800)}`;
  try {
    const response = await fetch(webhook, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, username: 'Fractured Realms' }),
    });
    if (!response.ok) return { ok: false, error: `Server returned ${response.status}` };
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err?.message ?? 'Network error' };
  }
});

ipcMain.handle('steam:reset-achievements', () => ({ ok: false, reason: 'disabled-in-build' }));

app.whenReady().then(() => {
  initSteam();
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

// Steam achievement bridge: the browser adapter delegates to this same service.
ipcMain.handle('steam:unlock', (_event, apiName) => {
  try {
    if (!steamClient) { console.log('[steam] unlock skipped, no client:', apiName); return { ok: false, reason: 'no-client' }; }
    if (typeof apiName !== 'string') return { ok: false, reason: 'bad-name' };
    const res = steamClient.achievement.activate(apiName);
    console.log('[steam] activate', apiName, '->', res);
    return { ok: true, activated: res };
  } catch (err) {
    console.log('[steam] activate ERROR', apiName, err?.message ?? err);
    return { ok: false, error: err?.message ?? String(err) };
  }
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
