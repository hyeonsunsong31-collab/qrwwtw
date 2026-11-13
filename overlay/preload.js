/*
 * overlay/preload.js -- the narrow bridge between the sandboxed renderer and the main process.
 * Exposes window.jarvis.* with only the specific channels the UI needs.
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('jarvis', {
  platform: process.platform,

  // per-session token + port for the local command bus (re-read on every reconnect)
  getToken: () => ipcRenderer.invoke('jarvis:token'),
  getSession: () => ipcRenderer.invoke('jarvis:session'),

  // the renderer's scripts are initialised (main shows the window and announces JARVIS)
  ready: () => ipcRenderer.send('hud:ready'),

  // HUD window control
  setClickThrough: (enabled) => ipcRenderer.send('hud:set-click-through', enabled),
  openSpace3d: () => ipcRenderer.send('space3d:open'),
  closeSpace3d: () => ipcRenderer.send('space3d:close'),
  setWallpaper: (enabled, state) => ipcRenderer.send('space3d:wallpaper', enabled, state),
  quit: () => ipcRenderer.send('app:quit'),

  // events pushed from main (global hotkeys, tray, wallpaper, start-up)
  onWake: (cb) => ipcRenderer.on('app:wake', () => cb()),
  onAnnounce: (cb) => ipcRenderer.on('app:announce', (_e, info) => cb(info || {})),
  onOpenSpace3d: (cb) => ipcRenderer.on('app:open-space3d', () => cb()),
  onToggleChat: (cb) => ipcRenderer.on('app:toggle-chat', () => cb()),
  onToggleGesture: (cb) => ipcRenderer.on('app:toggle-gesture', () => cb()),
  onOpenSettings: (cb) => ipcRenderer.on('app:open-settings', () => cb()),
  onWallpaperChanged: (cb) => ipcRenderer.on('wallpaper:changed', (_e, on) => cb(on)),
  onWallpaperState: (cb) => ipcRenderer.on('wallpaper:state', (_e, state) => cb(state)),
});
