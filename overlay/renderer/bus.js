/* bus.js -- the single command bus to the backend (requirements 15, 16).
 * Auto-reconnecting WebSocket with a tiny pub/sub. Every surface uses it, so voice and text,
 * on any screen, travel the same path. Each connection presents the per-session token the
 * backend writes to data/session.token (read through the preload bridge, re-read on every
 * reconnect so a backend restart is picked up automatically). Messages queue until open. */
const DEFAULT_PORT = 8765;

class Bus {
  constructor() { this.subs = {}; this.ws = null; this.connected = false; this.queue = []; this.connect(); }

  async connect() {
    let token = '', port = DEFAULT_PORT;
    try {
      const j = window.jarvis;
      if (j && j.getSession) { const s = (await j.getSession()) || {}; token = s.token || ''; port = s.port || DEFAULT_PORT; }
      else if (j) token = (await j.getToken()) || '';
    } catch (e) {}
    if (!token) return this._retry();              // backend not started yet
    try { this.ws = new WebSocket(`ws://127.0.0.1:${port}/ws?token=${encodeURIComponent(token)}`); }
    catch (e) { return this._retry(); }
    this.ws.onopen = () => {
      this.connected = true; this.emit('_open');
      this.queue.forEach(m => this.ws.send(m)); this.queue = [];
    };
    this.ws.onclose = () => { this.connected = false; this.emit('_close'); this._retry(); };
    this.ws.onerror = () => { try { this.ws.close(); } catch (e) {} };
    this.ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      this.emit(msg.type, msg); this.emit('*', msg);
    };
  }
  _retry() { clearTimeout(this._t); this._t = setTimeout(() => this.connect(), 1500); }

  on(type, cb) {
    (this.subs[type] = this.subs[type] || []).push(cb);
    return () => { this.subs[type] = (this.subs[type] || []).filter(f => f !== cb); };
  }
  emit(type, msg) { (this.subs[type] || []).forEach(cb => { try { cb(msg); } catch (e) { console.error(e); } }); }

  send(obj) {
    const s = JSON.stringify(obj);
    if (this.connected && this.ws && this.ws.readyState === 1) this.ws.send(s);
    else if (this.queue.length < 200) this.queue.push(s);
  }

  command(text, source = 'text') { if ((text || '').trim()) this.send({ type: 'command', text: text.trim(), source }); }
  wake() { this.send({ type: 'wake' }); }
  listen() { this.send({ type: 'listen' }); }
  interrupt() { this.send({ type: 'interrupt' }); }
  feedback(signals) { this.send({ type: 'feedback', signals }); }
  permission(request_id, granted) { this.send({ type: 'permission_response', request_id, granted }); }
  ui(action, args = {}) { this.send({ type: 'ui', action, args }); }
}

export const bus = new Bus();
