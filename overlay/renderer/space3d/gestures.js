/* space3d/gestures.js — camera hand-gesture control (requirement 5).
 *
 * Uses MediaPipe Tasks-Vision HandLandmarker (offline, from node_modules; assets fetched
 * by the installer). Computes:
 *   • pinch  — thumb-tip↔index-tip distance on one hand → drag the pointed object
 *   • spread — distance between two hands → scale the selected object / dolly camera
 *
 * The webcam is only opened while gestures are ON. toggle()/disable() let a button or a
 * voice command turn camera detection off (requirement 5). If MediaPipe, the model, or the
 * camera is unavailable, it degrades silently and mouse control still works.
 */
// Local paths are resolved against the PAGE (overlay/renderer/index.html), so they point at
// the repo's node_modules/ and models/ (installed offline by the setup script). A dynamic
// import() would otherwise resolve relative to this module's own folder.
const local = (p) => new URL(p, document.baseURI).href;
const WASM_PATHS = [
  local("../../node_modules/@mediapipe/tasks-vision/wasm"),
  "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.18/wasm",
];
const MODEL_PATHS = [
  local("../../models/hand_landmarker.task"),
  "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
];
const VISION_BUNDLES = [
  local("../../node_modules/@mediapipe/tasks-vision/vision_bundle.mjs"),
  "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.18/vision_bundle.mjs",
];

async function tryImport(paths) {
  for (const p of paths) { try { return await import(/* @vite-ignore */ p); } catch (e) {} }
  throw new Error("tasks-vision bundle not found");
}
async function firstOk(paths, make) {
  for (const p of paths) { try { return await make(p); } catch (e) {} }
  throw new Error("no working path");
}

export class Gestures {
  constructor(callbacks) {
    this.cb = callbacks;             // { onPinch(nx,ny,pinching), onTwoHand(scaleDelta) }
    this.enabled = false;
    this.landmarker = null;
    this.video = null;
    this.stream = null;
    this._raf = null;
    this._prevSpread = null;
    this._pinching = false;
  }

  async _ensure() {
    if (this.landmarker) return true;
    const vision = await tryImport(VISION_BUNDLES);
    const fileset = await firstOk(WASM_PATHS, (p) => vision.FilesetResolver.forVisionTasks(p));
    this.landmarker = await firstOk(MODEL_PATHS, (modelAssetPath) =>
      vision.HandLandmarker.createFromOptions(fileset, {
        baseOptions: { modelAssetPath },
        numHands: 2, runningMode: "VIDEO",
      }));
    return true;
  }

  async toggle() { return this.enabled ? (this.disable(), false) : (await this.enable(), this.enabled); }

  async enable() {
    try {
      await this._ensure();
      this.stream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 } });
      this.video = document.createElement("video");
      this.video.srcObject = this.stream; this.video.muted = true; this.video.playsInline = true;
      await this.video.play();
      this.enabled = true;
      this._loop();
    } catch (e) {
      console.error("gesture camera unavailable:", e);
      this.enabled = false;
      this.disable();
    }
    return this.enabled;
  }

  disable() {
    this.enabled = false;
    if (this._raf) cancelAnimationFrame(this._raf);
    if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
    this.stream = null; this.video = null; this._prevSpread = null; this._pinching = false;
  }

  _loop() {
    if (!this.enabled) return;
    this._raf = requestAnimationFrame(() => this._loop());
    if (!this.video || this.video.readyState < 2) return;
    let res;
    try { res = this.landmarker.detectForVideo(this.video, performance.now()); } catch (e) { return; }
    const hands = (res && res.landmarks) || [];

    if (hands.length >= 1) {
      const h = hands[0];
      const thumb = h[4], index = h[8];
      const pinchDist = Math.hypot(thumb.x - index.x, thumb.y - index.y);
      const pinching = pinchDist < 0.06;
      // midpoint of thumb+index, x mirrored to match a selfie-view camera
      const nx = 1 - (thumb.x + index.x) / 2;
      const ny = (thumb.y + index.y) / 2;
      this.cb.onPinch(nx, ny, pinching);
      this._pinching = pinching;
    }

    if (hands.length >= 2) {
      const a = hands[0][9], b = hands[1][9];     // palm centers
      const spread = Math.hypot(a.x - b.x, a.y - b.y);
      if (this._prevSpread != null && Math.abs(spread - this._prevSpread) > 0.004) {
        this.cb.onTwoHand(spread / this._prevSpread);
      }
      this._prevSpread = spread;
    } else {
      this._prevSpread = null;
    }
  }
}
