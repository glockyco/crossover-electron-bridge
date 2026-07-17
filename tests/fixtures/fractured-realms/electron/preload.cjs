const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  saveGame: (data) => ipcRenderer.invoke('save-game', data),
  submitFeedback: (payload) => ipcRenderer.invoke('submit-feedback', payload),
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
  // Unlock a Steam achievement by its API name (the UPPERCASE in-game id).
  steamUnlock: (apiName) => ipcRenderer.invoke('steam:unlock', apiName),
  // DEV only: wipe all Steam achievements/stats for re-testing.
  steamResetAchievements: () => ipcRenderer.invoke('steam:reset-achievements'),
  getFullscreen: () => ipcRenderer.invoke('get-fullscreen'),
  setFullscreen: (val) => ipcRenderer.invoke('set-fullscreen', val),
  quitApp: () => ipcRenderer.invoke('quit-app'),
  // Subscribe to fullscreen changes (Settings toggle, F11, OS controls).
  // Returns an unsubscribe function.
  onFullscreenChanged: (cb) => {
    const handler = (_e, val) => cb(val);
    ipcRenderer.on('fullscreen-changed', handler);
    return () => ipcRenderer.removeListener('fullscreen-changed', handler);
  },
});
