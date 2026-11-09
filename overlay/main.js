/*
 * overlay/main.js -- Electron main process for the JARVIS holographic overlay.
 *
 * HUD window: transparent, frameless, always-on-top, spanning the primary display, and
 * click-through except over holographic widgets. This is what keeps the emergency WAKE
 * button (requirement 9) and the on-wake buttons (requirement 10) above any app, web page,
 * or the desktop.
 *
 * Wallpaper window (requirement 18): a separate full-screen window that renders the 3D
 * project space and is attached BEHIND the desktop icons (Windows WorkerW layer, via
 * native/workerw.ps1). The HUD stays on top, so WAKE and the buttons remain usable.
 *
 * Also: single-instance lock, tray menu, Ctrl+Alt+J emergency wake, Ctrl+Alt+G gestures,
 * and the session-token hand-off (the renderer reads it through preload -> ipc).
 */
const { app, BrowserWindow, Tray, Menu, globalShortcut, ipcMain, screen, nativeImage } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');

const ROOT = path.join(__dirname, '..');
const TOKEN_FILE = path.join(ROOT, 'data', 'session.token');
const WORKERW = path.join(__dirname, 'native', 'workerw.ps1');

let win = null;        // HUD
let wallWin = null;    // live 3D wallpaper
let tray = null;
let clickThrough = true;

if (!app.requestSingleInstanceLock()) {
  app.quit();          // JARVIS is already running: the existing instance wakes instead
}

/* A crisp cyan arc-reactor glyph built as raw BGRA pixels (no binary asset to ship). */
function trayIcon() {
  const size = 32, buf = Buffer.alloc(size * size * 4), c = (size - 1) / 2;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const d = Math.hypot(x - c, y - c), i = (y * size + x) * 4;
      let a = 0;
      if (d >= 11 && d <= 14.5) a = 255;           // outer ring
      else if (d <= 5.5) a = 255;                  // core
      else if (d > 14.5 && d < 15.5) a = 110;      // soft edge
      if (a) { buf[i] = 255; buf[i + 1] = 225; buf[i + 2] = 56; buf[i + 3] = a; }
    }
  }
  return nativeImage.createFromBitmap(buf, { width: size, height: size, scaleFactor: 2 });
}

function primaryBounds() { return screen.getPrimaryDisplay().bounds; }

function webPrefs() {
  return { preload: path.join(__dirname, 'preload.js'), contextIsolation: true,
           nodeIntegration: false, sandbox: false };
}

function createHud() {
  const b = primaryBounds();
  win = new BrowserWindow({
    x: b.x, y: b.y, width: b.width, height: b.height,
    transparent: true, frame: false, resizable: false, movable: false,
    skipTaskbar: true, hasShadow: false, fullscreenable: true,
    backgroundColor: '#00000000', webPreferences: webPrefs(),
  });
  win.setAlwaysOnTop(true, 'screen-saver');           // above normal and full-screen apps
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.setIgnoreMouseEvents(true, { forward: true });   // click-through until a widget is hovered
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  screen.on('display-metrics-changed', () => {
    if (win && !win.isDestroyed() && !win.isFullScreen()) win.setBounds(primaryBounds());
  });
  win.on('closed', () => { win = null; });
}

/* ------------------------------------------------------------- IPC: HUD ---- */
ipcMain.handle('jarvis:token', () => {
  try { return fs.readFileSync(TOKEN_FILE, 'utf8').trim(); } catch (e) { return ''; }
});

ipcMain.on('hud:set-click-through', (_e, enabled) => {
  clickThrough = !!enabled;
  if (win && !win.isDestroyed() && !win.isFullScreen())
    win.setIgnoreMouseEvents(clickThrough, { forward: true });
});

ipcMain.on('space3d:open', () => {
  if (!win) return;
  win.setIgnoreMouseEvents(false);
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

/* ------------------------------------------------------ wallpaper window ---- */
function hwndOf(w) {
  const h = w.getNativeWindowHandle();
  return (h.length >= 8 ? h.readBigUInt64LE(0) : BigInt(h.readUInt32LE(0))).toString();
}

function runWorkerW(args) {
  if (process.platform !== 'win32') return null;
  return spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                  '-File', WORKERW, ...args], { windowsHide: true });
}

function openWallpaper(state) {
  if (wallWin && !wallWin.isDestroyed()) { wallWin.webContents.send('wallpaper:state', state); return; }
  const b = primaryBounds();
  wallWin = new BrowserWindow({
    x: b.x, y: b.y, width: b.width, height: b.height, frame: false, resizable: false,
    movable: false, skipTaskbar: true, focusable: false, show: false, hasShadow: false,
    backgroundColor: '#01080f', webPreferences: webPrefs(),
  });
  wallWin.setIgnoreMouseEvents(true);
  wallWin.loadFile(path.join(__dirname, 'renderer', 'wallpaper.html'));
  wallWin.once('ready-to-show', () => {
    wallWin.showInactive();
    wallWin.webContents.send('wallpaper:state', state);
    runWorkerW(['-Hwnd', hwndOf(wallWin)]);   // attach behind the icons (falls back to just above the desktop)
    if (win && !win.isDestroyed()) win.setAlwaysOnTop(true, 'screen-saver');
  });
  wallWin.on('closed', () => { wallWin = null; });
}

function closeWallpaper() {
  if (!wallWin) return;
  wallWin.destroy();
  wallWin = null;
  runWorkerW(['-Detach']);                      // repaint the user's normal wallpaper
}

ipcMain.on('space3d:wallpaper', (_e, enabled, state) => {
  if (enabled) openWallpaper(state || null); else closeWallpaper();
  if (win && !win.isDestroyed()) win.webContents.send('wallpaper:changed', !!enabled);
});

ipcMain.on('app:quit', () => app.quit());

/* ------------------------------------------------------------ wake / tray --- */
function wake() {
  if (win && !win.isDestroyed()) { win.showInactive(); win.webContents.send('app:wake'); }
}

function buildTray() {
  tray = new Tray(trayIcon());
  tray.setToolTip('JARVIS');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Wake JARVIS', click: wake },
    { label: 'Open 3D project space', click: () => win && win.webContents.send('app:open-space3d') },
    { label: 'Toggle chat', click: () => win && win.webContents.send('app:toggle-chat') },
    { label: 'Settings', click: () => win && win.webContents.send('app:open-settings') },
    { type: 'separator' },
    { label: 'Quit overlay', click: () => app.quit() },
  ]));
  tray.on('click', wake);
}

app.on('second-instance', wake);

app.whenReady().then(() => {
  createHud();
  buildTray();
  globalShortcut.register('Control+Alt+J', wake);
  globalShortcut.register('Control+Alt+G', () => win && win.webContents.send('app:toggle-gesture'));
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createHud(); });
});

app.on('will-quit', () => { globalShortcut.unregisterAll(); closeWallpaper(); });
app.on('window-all-closed', () => { /* stay resident; the supervisor manages lifetime */ });
