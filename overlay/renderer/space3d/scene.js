/* space3d/scene.js — the 3D modelling project space (requirements 5, 17, 18).
 *
 * Interactions, all on the same objects:
 *   • drag       — left-click drag  OR  a camera "pinch" gesture (thumb+index)
 *   • zoom/scale — mouse wheel over an object scales it; over empty space dollies the
 *                  camera; a two-hand spread/pinch gesture also scales (Iron-Man style)
 *   • clone      — duplicate the selected object (button / voice / text)
 * Camera orbit/pan via OrbitControls (zoom handled manually so wheel can scale objects).
 * Gesture detection is lazy-loaded and can be toggled off by button or voice (requirement 5).
 * Wallpaper mode (requirement 18) is applied via wallpaper.js + the main process.
 */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { makeHoloObject, setSelected } from "./objects.js";
import { applyWallpaperVisual } from "./wallpaper.js";

export class Space3D {
  constructor(container) {
    this.container = container;
    this.objects = [];
    this.selected = null;
    this.running = false;
    this.gestures = null;
    this.dragging = false;
    this._dragPlane = new THREE.Plane();
    this._dragOffset = new THREE.Vector3();
    this._ray = new THREE.Raycaster();
    this._ndc = new THREE.Vector2();
  }

  async init() {
    const w = this.container.clientWidth || window.innerWidth;
    const h = this.container.clientHeight || window.innerHeight;

    this.scene = new THREE.Scene();
    this.scene.fog = new THREE.FogExp2(0x03121b, 0.035);

    this.camera = new THREE.PerspectiveCamera(55, w / h, 0.1, 200);
    this.camera.position.set(0, 2.4, 7);

    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    this.renderer.setSize(w, h);
    this.container.appendChild(this.renderer.domElement);

    this.scene.add(new THREE.AmbientLight(0x66ccff, 0.5));
    const key = new THREE.PointLight(0x9fe8ff, 1.2, 60); key.position.set(6, 10, 8);
    this.scene.add(key);

    const grid = new THREE.GridHelper(40, 40, 0x1fb6d6, 0x0d4555);
    grid.material.transparent = true; grid.material.opacity = 0.35; grid.position.y = -1.5;
    this.scene.add(grid);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.enableZoom = false;         // wheel is used to scale objects / dolly manually
    this.controls.target.set(0, 0.5, 0);

    // starter objects
    ["box", "sphere", "torus", "icosa"].forEach((t, i) => {
      const o = makeHoloObject(t);
      o.position.set((i - 1.5) * 2.1, 0.4, 0);
      this.scene.add(o); this.objects.push(o);
    });

    this._bindInput();
    this._onResize = () => this.resize();
    window.addEventListener("resize", this._onResize);
  }

  /* ----------------------------------------------------------------- input ---- */
  _bindInput() {
    const el = this.renderer.domElement;
    el.addEventListener("pointerdown", (e) => this._onDown(e));
    el.addEventListener("pointermove", (e) => this._onMove(e));
    window.addEventListener("pointerup", () => this._onUp());
    el.addEventListener("wheel", (e) => this._onWheel(e), { passive: false });
    el.addEventListener("dblclick", () => this.addObject()); // quick add
  }

  _setNdc(x, y) {
    const r = this.renderer.domElement.getBoundingClientRect();
    this._ndc.set(((x - r.left) / r.width) * 2 - 1, -((y - r.top) / r.height) * 2 + 1);
  }

  _pick() {
    this._ray.setFromCamera(this._ndc, this.camera);
    const hits = this._ray.intersectObjects(this.objects, true);
    if (!hits.length) return null;
    let g = hits[0].object; while (g && !g.userData.selectable) g = g.parent;
    return g || null;
  }

  select(group) {
    if (this.selected) setSelected(this.selected, false);
    this.selected = group;
    if (group) setSelected(group, true);
  }

  _beginDrag(group) {
    this.dragging = true;
    this.controls.enabled = false;
    const camDir = new THREE.Vector3();
    this.camera.getWorldDirection(camDir);
    this._dragPlane.setFromNormalAndCoplanarPoint(camDir, group.position);
    const hit = new THREE.Vector3();
    if (this._ray.ray.intersectPlane(this._dragPlane, hit)) this._dragOffset.copy(group.position).sub(hit);
  }

  _onDown(e) {
    this._setNdc(e.clientX, e.clientY);
    this._ray.setFromCamera(this._ndc, this.camera);
    const g = this._pick();
    this.select(g);
    if (g) this._beginDrag(g);
  }

  _onMove(e) {
    if (!this.dragging || !this.selected) return;
    this._setNdc(e.clientX, e.clientY);
    this._ray.setFromCamera(this._ndc, this.camera);
    const hit = new THREE.Vector3();
    if (this._ray.ray.intersectPlane(this._dragPlane, hit))
      this.selected.position.copy(hit.add(this._dragOffset));
  }

  _onUp() { this.dragging = false; this.controls.enabled = true; }

  _onWheel(e) {
    this._setNdc(e.clientX, e.clientY);
    this._ray.setFromCamera(this._ndc, this.camera);
    const g = this._pick();
    if (g) {                                   // scale the pointed object
      e.preventDefault();
      const f = e.deltaY < 0 ? 1.1 : 1 / 1.1;
      g.scale.multiplyScalar(f);
      g.scale.clampScalar(0.1, 12);
      this.select(g);
    } else {                                   // dolly the camera
      const dir = new THREE.Vector3().subVectors(this.camera.position, this.controls.target);
      dir.multiplyScalar(e.deltaY < 0 ? 0.9 : 1.1);
      this.camera.position.copy(this.controls.target).add(dir);
    }
  }

  /* --------------------------------------------------------------- actions ---- */
  addObject(type) {
    const o = makeHoloObject(type);
    o.position.set((Math.random() - 0.5) * 3, 0.6, (Math.random() - 0.5) * 2);
    this.scene.add(o); this.objects.push(o); this.select(o);
    return o;
  }

  clone() {
    const src = this.selected || this.objects[this.objects.length - 1];
    if (!src) return null;
    const c = makeHoloObject(src.userData.type);
    c.position.copy(src.position).add(new THREE.Vector3(1.2, 0, 0));
    c.scale.copy(src.scale); c.quaternion.copy(src.quaternion);
    this.scene.add(c); this.objects.push(c); this.select(c);
    return c;
  }

  deleteSelected() {
    if (!this.selected) return;
    this.scene.remove(this.selected);
    this.objects = this.objects.filter((o) => o !== this.selected);
    this.selected = null;
  }

  /* -------------------------------------------------------- gesture control --- */
  async toggleGesture() {
    if (!this.gestures) {
      try {
        const mod = await import("./gestures.js");
        this.gestures = new mod.Gestures({
          onPinch: (nx, ny, pinching) => this._gesturePinch(nx, ny, pinching),
          onTwoHand: (scaleDelta) => this._gestureScale(scaleDelta),
        });
      } catch (e) { console.error("gestures unavailable:", e); return false; }
    }
    const on = await this.gestures.toggle();
    return on;
  }

  _gesturePinch(nx, ny, pinching) {
    // nx, ny are normalized [0,1] from the camera feed (mirrored x)
    const r = this.renderer.domElement.getBoundingClientRect();
    this._setNdc(r.left + nx * r.width, r.top + ny * r.height);
    this._ray.setFromCamera(this._ndc, this.camera);
    if (pinching && !this.dragging) { const g = this._pick(); if (g) { this.select(g); this._beginDrag(g); } }
    else if (pinching && this.dragging && this.selected) {
      const hit = new THREE.Vector3();
      if (this._ray.ray.intersectPlane(this._dragPlane, hit))
        this.selected.position.copy(hit.add(this._dragOffset));
    } else if (!pinching) { this._onUp(); }
  }

  _gestureScale(delta) {
    const g = this.selected || this.objects[this.objects.length - 1];
    if (g) { g.scale.multiplyScalar(delta); g.scale.clampScalar(0.1, 12); }
    else {
      const dir = new THREE.Vector3().subVectors(this.camera.position, this.controls.target);
      dir.multiplyScalar(1 / delta); this.camera.position.copy(this.controls.target).add(dir);
    }
  }

  /* ---------------------------------------------------------- wallpaper mode -- */
  setWallpaper(on) {
    this.wallpaper = on;
    this.controls.enabled = !on;               // as a wallpaper it shouldn't grab the mouse
    applyWallpaperVisual(this.renderer, this.scene, on);
  }

  /* ------------------------------------------------------------- lifecycle ---- */
  resize() {
    const w = this.container.clientWidth || window.innerWidth;
    const h = this.container.clientHeight || window.innerHeight;
    this.camera.aspect = w / h; this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h);
  }

  open() { if (this.running) return; this.running = true; this.resize(); this._loop(); }

  close() {
    this.running = false;
    if (this.gestures) this.gestures.disable();
  }

  _loop() {
    if (!this.running) return;
    requestAnimationFrame(() => this._loop());
    const t = performance.now() * 0.001;
    // gentle idle rotation so the scene feels alive
    this.objects.forEach((o, i) => { if (o !== this.selected || !this.dragging) o.rotation.y += 0.002 + i * 0.0003; });
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }

  dispose() {
    this.close();
    window.removeEventListener("resize", this._onResize);
    if (this.renderer) { this.renderer.dispose(); this.renderer.domElement.remove(); }
  }
}
