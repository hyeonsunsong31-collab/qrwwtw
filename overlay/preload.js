/*
 * overlay/preload.js — safe bridge between the sandboxed renderer and the main process.
 * Exposes window.jarvis.* with only the specific channels the UI needs.
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('jarvis', {
  platform: process.platform,

  // window / overlay control
  setClickThrough: (enabled) => ipcRenderer.send('hud:set-click-through', enabled),
  openSpace3d: () => ipcRenderer.send('space3d:open'),
  closeSpace3d: () => ipcRenderer.send('space3d:close'),
  setWallpaper: (enabled) => ipcRenderer.send('space3d:wallpaper', enabled),
  quit: () => ipcRenderer.send('app:quit'),

  // events pushed from main (global hotkey, tray)
  onWake: (cb) => ipcRenderer.on('app:wake', () => cb()),
  onOpenSpace3d: (cb) => ipcRenderer.on('app:open-space3d', () => cb()),
  onToggleChat: (cb) => ipcRenderer.on('app:toggle-chat', () => cb()),
  onToggleGesture: (cb) => ipcRenderer.on('app:toggle-gesture', () => cb()),
});
