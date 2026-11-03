/*
 * overlay/main.js — Electron main process for the JARVIS holographic overlay.
 *
 * Creates ONE transparent, frameless, always-on-top window that spans the primary
 * display and is click-through by default (clicks pass to the apps beneath), except
 * over interactive holographic widgets. This is what makes the emergency wake button
 * (requirement 9) and the on-wake button cluster (requirement 10) float above any app,
 * web page, or the desktop.
 *
 * Window modes (driven by IPC from the renderer):
 *   normal     — small HUD: always-visible emergency wake button + state ring + chat toggle
 *   space3d    — fullscreen interactive 3D project space that visually replaces the
 *                desktop (requirement 18) and shows every feature button (requirement 6)
 *   wallpaper  — sends the 3D scene behind the icons as a live wallpaper (best-effort;
 *                see space3d/wallpaper.js for the Windows WorkerW note)
 *
 * Global hotkey Ctrl+Alt+J = emergency wake (requirement 9, a keyboard route that works
 * even if the mic is unavailable). Tray icon gives the same actions.
 */
const { app, BrowserWindow, Tray, Menu, globalShortcut, ipcMain, screen, nativeImage } = require('electron');
const path = require('path');

let win = null;
let tray = null;
let clickThrough = true;

// A tiny cyan reactor glyph for the tray, embedded so there's no binary asset to ship.
const TRAY_ICON = nativeImage.createFromDataURL(
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAcklEQVR4nGNgGAWj' +
  'YBSMglEwCkbBKBgFo2AUjIJRMApGwSgYBaNgFIyCUTAKRsEoGAWjYBSMglEwCkbBKBgFo2AUjIJRMApGwSgY' +
  'BaNgFIyCUTAKRsEoGAWjYBSMglEwCkbBKBgFo2AUjIJRMAoAAwAZ5wFd0m2q0wAAAABJRU5ErkJggg==');

function primaryBounds() {
  return screen.getPrimaryDisplay().bounds;
}

function createWindow() {
  const b = primaryBounds();
  win = new BrowserWindow({
    x: b.x, y: b.y, width: b.width, height: b.height,
    transparent: true, frame: false, resizable: false, movable: false,
    skipTaskbar: true, hasShadow: false, fullscreenable: true,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true, nodeIntegration: false, sandbox: false,
    },
  });

  win.setAlwaysOnTop(true, 'screen-saver');       // above normal and fullscreen apps
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.setIgnoreMouseEvents(true, { forward: true }); // click-through until a widget is hovered
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  // keep covering the primary display if its resolution changes
  screen.on('display-metrics-changed', () => {
    if (!win || win.isDestroyed()) return;
    const nb = primaryBounds();
    if (!win.isFullScreen()) win.setBounds(nb);
  });

  win.on('closed', () => { win = null; });
}

// Renderer toggles click-through as the pointer enters/leaves holographic widgets.
ipcMain.on('hud:set-click-through', (_e, enabled) => {
  clickThrough = !!enabled;
  if (win && !win.isDestroyed()) win.setIgnoreMouseEvents(clickThrough, { forward: true });
});

ipcMain.on('space3d:open', () => {
  if (!win) return;
  win.setIgnoreMouseEvents(false);       // fully interactive
  win.setFullScreen(true);
  win.focus();
});

ipcMain.on('space3d:close', () => {
  if (!win) return;
  win.setFullScreen(false);
  win.setBounds(primaryBounds());
  win.setAlwaysOnTop(true, 'screen-saver');
  win.setIgnoreMouseEvents(clickThrough, { forward: true });
});

// Wallpaper mode: drop behind everything (requirement 18). True behind-icons embedding on
// Windows needs WorkerW (documented in space3d/wallpaper.js); here we send-to-bottom.
ipcMain.on('space3d:wallpaper', (_e, enabled) => {
  if (!win) return;
  if (enabled) {
    win.setAlwaysOnTop(false);
    win.setFullScreen(true);
    if (process.platform === 'darwin') app.dock && app.dock.hide();
    win.moveTop();           // ensure visible, then push under active windows
    win.blur();
  } else {
    win.setAlwaysOnTop(true, 'screen-saver');
  }
});

ipcMain.on('app:quit', () => { app.quit(); });

function wake() {
  if (win && !win.isDestroyed()) {
    win.webContents.send('app:wake');
    win.showInactive();
  }
}

function buildTray() {
  tray = new Tray(TRAY_ICON);
  tray.setToolTip('JARVIS');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Wake JARVIS', click: wake },
    { label: 'Open 3D project space', click: () => win && win.webContents.send('app:open-space3d') },
    { label: 'Toggle chat', click: () => win && win.webContents.send('app:toggle-chat') },
    { type: 'separator' },
    { label: 'Quit', click: () => app.quit() },
  ]));
  tray.on('click', wake);
}

app.whenReady().then(() => {
  createWindow();
  buildTray();
  globalShortcut.register('Control+Alt+J', wake);                       // emergency wake
  globalShortcut.register('Control+Alt+G', () => win && win.webContents.send('app:toggle-gesture'));
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});

app.on('will-quit', () => globalShortcut.unregisterAll());
// Overlay is a background companion: keep running when the window is closed.
app.on('window-all-closed', (e) => { /* stay resident */ });
