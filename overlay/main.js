/*
 * overlay/main.js -- Electron main process for the JARVIS holographic overlay.
 *
 * HUD window: transparent, frameless, always-on-top, covering the primary display's work
 * area, and click-through except over holographic widgets. This is what keeps the emergency
 * WAKE button (requirement 9) and the on-wake buttons (requirement 10) above any app, web
 * page, or the desktop.
 *
 * Wallpaper window (requirement 18): a separate full-screen window that renders the 3D
 * project space and is attached BEHIND the desktop icons (Windows WorkerW layer, via
 * native/workerw.ps1). The HUD stays on top, so WAKE and the buttons remain usable.
 *
 * Never silent: every start-up step, load failure, renderer crash and script error is
 * written to data/logs/overlay-main.log, and data/overlay.json tells START_JARVIS.bat
 * whether the HUD really is on screen. When the HUD is ready it announces itself.
 *
 * Also: single-instance lock, tray menu, Ctrl+Alt+J emergency wake, Ctrl+Alt+G gestures,
 * and the session hand-off (the renderer reads token + port through preload -> ipc).
 */
const { app, BrowserWindow, Tray, Menu, globalShortcut, ipcMain, screen, nativeImage } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');

const ROOT = path.join(__dirname, '..');
const DATA = process.env.JARVIS_DATA || path.join(ROOT, 'data');
const LOG_DIR = path.join(DATA, 'logs');
const LOG_FILE = path.join(LOG_DIR, 'overlay-main.log');
const TOKEN_FILE = path.join(DATA, 'session.token');
const STATE_FILE = path.join(DATA, 'overlay.json');
const SAFE_FLAG = path.join(DATA, 'overlay.safe-graphics');
const CONFIG_FILE = process.env.JARVIS_CONFIG || path.join(ROOT, 'config', 'jarvis.config.json');
const WORKERW = path.join(__dirname, 'native', 'workerw.ps1');
const QUIT_CODE = 42;          // tells the supervisor "the user quit JARVIS" (do not restart)

let win = null;        // HUD
let wallWin = null;    // live 3D wallpaper
let tray = null;
let clickThrough = true;
let loadRetries = 0;
let readyTimer = null;
let gpuCrashes = 0;
let lastConsole = '';
let consoleBudget = 200;       // renderer messages logged per run (a broken page must not fill the disk)

/* ------------------------------------------------------------------ logging ---- */
function log(msg) {
  const line = `[${new Date().toISOString()}] ${msg}\n`;
  try { fs.appendFileSync(LOG_FILE, line); } catch (e) { /* logging must never break the HUD */ }
}
try {
  fs.mkdirSync(LOG_DIR, { recursive: true });
  if (fs.existsSync(LOG_FILE) && fs.statSync(LOG_FILE).size > 1024 * 1024) fs.renameSync(LOG_FILE, LOG_FILE + '.1');
} catch (e) { /* ignore */ }

function readConfig() {
  try { return JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8')); } catch (e) { return {}; }
}
const config = readConfig();
const safeForced = process.env.JARVIS_SAFE_GRAPHICS === '1' || !!(config.overlay && config.overlay.safe_graphics);
const safeGraphics = safeForced || fs.existsSync(SAFE_FLAG);

const state = { pid: process.pid, startedAt: Date.now(), electron: process.versions.electron,
                platform: process.platform, safeGraphics, loaded: false, visible: false };

let stateRetry = null;
function writeState(extra, attempt) {
  Object.assign(state, extra || {});
  if (win && !win.isDestroyed()) { state.visible = win.isVisible(); state.bounds = win.getBounds(); }
  else state.visible = false;
  state.updatedAt = Date.now();
  try {
    fs.writeFileSync(STATE_FILE + '.tmp', JSON.stringify(state));
    fs.renameSync(STATE_FILE + '.tmp', STATE_FILE);
  } catch (e) {
    // On Windows the rename fails while another program (START's launcher) has the file open
    // for a moment. The state must not be lost, so try again shortly.
    const n = attempt || 0;
    if (n < 6) { clearTimeout(stateRetry); stateRetry = setTimeout(() => writeState(null, n + 1), 250); }
    else log('could not write overlay.json: ' + e.message);
  }
}
setInterval(() => writeState(), 10000).unref();     // heartbeat: the file always reflects the present

process.on('uncaughtException', (err) => {
  log('FATAL uncaught exception: ' + ((err && err.stack) || err));
  writeState({ lastError: String((err && err.message) || err) });
  app.exit(1);                                   // the supervisor starts a clean instance
});
process.on('unhandledRejection', (reason) => {
  log('unhandled rejection: ' + ((reason && reason.stack) || reason));
});

log(`overlay starting: electron ${process.versions.electron}, pid ${process.pid}, `
  + `${process.platform} ${process.arch}, safe graphics ${safeGraphics ? 'ON' : 'off'}`);

// Software rendering for PCs whose graphics driver cannot composite a transparent window.
if (safeGraphics) app.disableHardwareAcceleration();

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  log('another overlay instance is already running; this one exits');
  app.exit(0);
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

/* The HUD covers the work area (the screen minus the taskbar), not the whole screen: an
 * always-on-top window of exactly screen size is treated by Windows as a full-screen app
 * (notifications are silenced, the taskbar drops behind it), and the WAKE button would sit
 * on top of the taskbar. If the taskbar auto-hides, stay one pixel short of full screen. */
function hudBounds() {
  const d = screen.getPrimaryDisplay(), w = d.workArea, b = d.bounds;
  const full = w.width === b.width && w.height === b.height;
  return { x: w.x, y: w.y, width: w.width, height: full ? w.height - 1 : w.height };
}

function webPrefs() {
  return { preload: path.join(__dirname, 'preload.js'), contextIsolation: true,
           nodeIntegration: false, sandbox: false };
}

function showHud(why) {
  if (!win || win.isDestroyed()) return;
  if (!win.isVisible()) {
    win.showInactive();                                // visible without stealing focus
    log(`HUD shown (${why}): ${JSON.stringify(win.getBounds())}`);
  }
  win.setAlwaysOnTop(true, 'screen-saver');            // above normal and full-screen apps
  win.moveTop();
  writeState();
}

function loadHud() {
  win.loadFile(path.join(__dirname, 'renderer', 'index.html')).catch((e) => log('loadFile rejected: ' + e.message));
}

function createHud() {
  const b = hudBounds();
  const d = screen.getPrimaryDisplay();
  writeState({ display: { width: d.bounds.width, height: d.bounds.height, scaleFactor: d.scaleFactor } });
  win = new BrowserWindow({
    x: b.x, y: b.y, width: b.width, height: b.height, show: false,
    transparent: true, frame: false, resizable: false, movable: false,
    skipTaskbar: true, hasShadow: false, fullscreenable: true,
    backgroundColor: '#00000000', webPreferences: webPrefs(),
  });
  win.setAlwaysOnTop(true, 'screen-saver');
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.setIgnoreMouseEvents(true, { forward: true });   // click-through until a widget is hovered

  const wc = win.webContents;
  win.once('ready-to-show', () => showHud('ready-to-show'));
  wc.on('did-finish-load', () => {
    log('HUD page loaded');
    showHud('did-finish-load');
    clearTimeout(readyTimer);
    readyTimer = setTimeout(() => {                     // the page loaded but its scripts never ran
      if (state.loaded || !win || win.isDestroyed()) return;
      log('HUD scripts did not initialise within 15 s; reloading the page');
      if (loadRetries++ < 3) wc.reloadIgnoringCache();
    }, 15000);
  });
  wc.on('did-fail-load', (_e, code, desc, url, isMainFrame) => {
    if (!isMainFrame) return;
    log(`HUD page failed to load (${code} ${desc}) ${url}`);
    writeState({ loaded: false, lastError: `page failed to load: ${desc}` });
    if (loadRetries++ < 5) setTimeout(() => { if (win && !win.isDestroyed()) loadHud(); }, 2000);
  });
  wc.on('render-process-gone', (_e, details) => {
    log(`HUD renderer process gone (${details.reason}, exit code ${details.exitCode}); reloading`);
    writeState({ loaded: false, lastError: `renderer ${details.reason}` });
    if (details.reason !== 'clean-exit' && loadRetries++ < 8)
      setTimeout(() => { if (win && !win.isDestroyed()) loadHud(); }, 1000);
  });
  wc.on('console-message', (_e, level, message, line, source) => {
    if (level < 2 || consoleBudget <= 0) return;        // warnings and errors only
    const text = String(message).replace(/token=[^&\s'"]+/g, 'token=***').slice(0, 500);
    if (text === lastConsole || /Electron Security Warning/.test(text)) return;
    lastConsole = text; consoleBudget--;
    log(`renderer ${level === 3 ? 'error' : 'warning'}: ${text} (${path.basename(String(source || ''))}:${line})`);
  });
  win.on('unresponsive', () => {
    log('HUD window is not responding; reloading');
    if (loadRetries++ < 8) wc.reloadIgnoringCache();
  });
  win.on('show', () => writeState());
  win.on('hide', () => writeState());
  win.on('closed', () => { win = null; writeState({ loaded: false }); });

  setTimeout(() => showHud('fallback timer'), 4000);    // never stay hidden if an event is missed
  loadHud();

  const reposition = () => {
    if (win && !win.isDestroyed() && !win.isFullScreen()) { win.setBounds(hudBounds()); writeState(); }
  };
  screen.on('display-metrics-changed', reposition);
  screen.on('display-added', reposition);
  screen.on('display-removed', reposition);
}

/* ------------------------------------------------------------- IPC: HUD ---- */
function readToken() {
  try { return fs.readFileSync(TOKEN_FILE, 'utf8').trim(); } catch (e) { return ''; }
}
function backendPort() {
  const p = Number(readConfig().server && readConfig().server.port);
  return Number.isInteger(p) && p > 0 && p < 65536 ? p : 8765;
}
ipcMain.handle('jarvis:token', () => readToken());
ipcMain.handle('jarvis:session', () => ({ token: readToken(), port: backendPort() }));

function announce(reason) {
  if (win && !win.isDestroyed()) win.webContents.send('app:announce', { reason });
}

// the renderer's scripts are up: this is the real "the HUD works" signal
ipcMain.on('hud:ready', () => {
  clearTimeout(readyTimer);
  loadRetries = 0;
  log('HUD ready (renderer initialised)');
  showHud('renderer ready');
  writeState({ loaded: true, readyAt: Date.now(), lastError: null });
  announce('startup');
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
  win.setBounds(hudBounds());
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
  const child = spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                         '-File', WORKERW, ...args], { windowsHide: true });
  child.on('error', (e) => log('workerw.ps1 could not be started: ' + e.message));
  return child;
}

function openWallpaper(wallState) {
  if (wallWin && !wallWin.isDestroyed()) { wallWin.webContents.send('wallpaper:state', wallState); return; }
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
    wallWin.webContents.send('wallpaper:state', wallState);
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

ipcMain.on('space3d:wallpaper', (_e, enabled, wallState) => {
  if (enabled) openWallpaper(wallState || null); else closeWallpaper();
  if (win && !win.isDestroyed()) win.webContents.send('wallpaper:changed', !!enabled);
});

/* Quit JARVIS as a whole: the supervisor sees this exit code and stops the backend too
 * (any other exit is treated as a crash and the overlay is started again). */
function quitJarvis() {
  log('Quit JARVIS chosen');
  try { globalShortcut.unregisterAll(); closeWallpaper(); } catch (e) { /* ignore */ }
  writeState({ loaded: false, quitAt: Date.now() });
  app.exit(QUIT_CODE);
}
ipcMain.on('app:quit', quitJarvis);

/* Restart the overlay only (the supervisor starts it again; standalone it relaunches itself). */
function restartOverlay() {
  if (process.env.JARVIS_SUPERVISED === '1') return app.exit(1);
  app.relaunch();
  app.exit(0);
}

/* Software rendering on/off from the tray, for PCs whose graphics driver shows the HUD
 * black, flickering or not at all. START_JARVIS_SAFE_GRAPHICS.bat sets the same flag. */
function toggleSafeGraphics() {
  try {
    if (fs.existsSync(SAFE_FLAG)) fs.rmSync(SAFE_FLAG, { force: true });
    else fs.writeFileSync(SAFE_FLAG, 'set from the tray menu\n');
  } catch (e) { log('could not change the graphics mode: ' + e.message); return; }
  log('graphics mode changed from the tray; restarting the overlay');
  restartOverlay();
}

/* ------------------------------------------------------------ wake / tray --- */
function wake() {
  if (win && !win.isDestroyed()) { showHud('wake'); win.webContents.send('app:wake'); }
}

function buildTray() {
  tray = new Tray(trayIcon());
  tray.setToolTip('JARVIS');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Wake JARVIS', click: wake },
    { label: 'Show the HUD', click: () => { showHud('tray'); announce('tray'); } },
    { label: 'Open 3D project space', click: () => win && win.webContents.send('app:open-space3d') },
    { label: 'Toggle chat', click: () => win && win.webContents.send('app:toggle-chat') },
    { label: 'Settings', click: () => win && win.webContents.send('app:open-settings') },
    { type: 'separator' },
    { label: 'Software rendering (if the HUD is black or missing)', type: 'checkbox',
      checked: safeGraphics, enabled: !safeForced, click: toggleSafeGraphics },
    { label: 'Quit JARVIS', click: quitJarvis },
  ]));
  tray.on('click', wake);
}

// Starting the overlay a second time just brings the running HUD forward.
app.on('second-instance', () => { showHud('second instance'); announce('second-instance'); });

app.on('child-process-gone', (_e, details) => {
  log(`child process gone: ${details.type} (${details.reason}, exit code ${details.exitCode})`);
  if (details.type !== 'GPU' || safeGraphics || details.reason === 'clean-exit') return;
  if (++gpuCrashes >= 2 && Date.now() - state.startedAt < 120000) {
    // the graphics driver cannot run the HUD: remember that and come back with software rendering
    try { fs.writeFileSync(SAFE_FLAG, 'set automatically: the GPU process crashed repeatedly\n'); } catch (e) { /* ignore */ }
    log('GPU process crashed repeatedly; switching to software rendering and restarting');
    app.exit(1);
  }
});

app.whenReady().then(() => {
  if (!gotLock) return;
  createHud();
  try { buildTray(); } catch (e) { log('tray icon could not be created: ' + e.message); }
  if (!globalShortcut.register('Control+Alt+J', wake)) log('hotkey Ctrl+Alt+J is taken by another program');
  if (!globalShortcut.register('Control+Alt+G', () => win && win.webContents.send('app:toggle-gesture')))
    log('hotkey Ctrl+Alt+G is taken by another program');
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createHud(); });
}).catch((e) => {
  log('FATAL start-up error: ' + ((e && e.stack) || e));
  writeState({ lastError: String((e && e.message) || e) });
  app.exit(1);
});

app.on('will-quit', () => {
  try { globalShortcut.unregisterAll(); closeWallpaper(); } catch (e) { /* ignore */ }
  writeState({ loaded: false });
});
app.on('window-all-closed', () => { /* stay resident; the supervisor manages lifetime */ });
