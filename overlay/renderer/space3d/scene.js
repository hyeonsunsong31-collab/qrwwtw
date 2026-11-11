/* space3d/scene.js -- the 3D modelling project space (requirements 5, 17, 18).
 *
 * Interactions, all on the same objects:
 *   - drag       left-click drag  OR  a camera "pinch" gesture (thumb + index finger)
 *   - zoom/scale mouse wheel over an object scales it; over empty space dollies the camera;
 *                spreading / closing two hands scales too (Iron-Man style)
 *   - clone      duplicate the selected object (button / voice / text)
 * Plus: save / load the project, export a binary glTF (.glb), a live point cloud of the
 * neural mesh, and a non-interactive wallpaper mode with a slow orbit.
 * Gesture detection is lazy-loaded and can be turned off by button or voice.
 */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { makeHoloObject, setSelected } from "./objects.js";
import { applyWallpaperVisual } from "./wallpaper.js";

const MODULE_COLORS = [0x38e1ff, 0xffcf6b, 0xff7ad9, 0x7dff9a, 0xa58bff, 0xff8f5a];

export class Space3D {
  constructor(container, options = {}) {
    this.container = container;
    this.interactive = options.interactive !== false;
    this.objects = [];
    this.selected = null;
    this.running = false;
    this.gestures = null;
    this.dragging = false;
    this.autoOrbit = false;
    this.wallpaper = false;
    this.neural = null;
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
    this.world = new THREE.Group();          // user objects only (what save/export include)
    this.world.name = "jarvis-project";
    this.scene.add(this.world);

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
    this.controls.enableZoom = false;         // the wheel scales objects / dollies manually
    this.controls.target.set(0, 0.5, 0);
    this.controls.enabled = this.interactive;

    ["box", "sphere", "torus", "icosa"].forEach((t, i) => {
      const o = this._add(makeHoloObject(t));
      o.position.set((i - 1.5) * 2.1, 0.4, 0);
    });

    if (this.interactive) this._bindInput();
    this._onResize = () => this.resize();
    window.addEventListener("resize", this._onResize);
  }

  _add(group) { this.world.add(group); this.objects.push(group); return group; }

  /* ------------------------------------------------------------------ input ---- */
  _bindInput() {
    const el = this.renderer.domElement;
    el.addEventListener("pointerdown", (e) => this._onDown(e));
    el.addEventListener("pointermove", (e) => this._onMove(e));
    window.addEventListener("pointerup", () => this._onUp());
    el.addEventListener("wheel", (e) => this._onWheel(e), { passive: false });
    el.addEventListener("dblclick", () => this.addObject());
  }

  _setNdc(x, y) {
    const r = this.renderer.domElement.getBoundingClientRect();
    this._ndc.set(((x - r.left) / r.width) * 2 - 1, -((y - r.top) / r.height) * 2 + 1);
    this._ray.setFromCamera(this._ndc, this.camera);
  }

  _pick() {
    const hits = this._ray.intersectObjects(this.objects, true);
    if (!hits.length) return null;
    let g = hits[0].object;
    while (g && !(g.userData && g.userData.selectable)) g = g.parent;
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

  _dragTo() {
    const hit = new THREE.Vector3();
    if (this.selected && this._ray.ray.intersectPlane(this._dragPlane, hit))
      this.selected.position.copy(hit.add(this._dragOffset));
  }

  _onDown(e) { this._setNdc(e.clientX, e.clientY); const g = this._pick(); this.select(g); if (g) this._beginDrag(g); }
  _onMove(e) { if (!this.dragging) return; this._setNdc(e.clientX, e.clientY); this._dragTo(); }
  _onUp() { this.dragging = false; this.controls.enabled = this.interactive && !this.wallpaper; }

  _onWheel(e) {
    this._setNdc(e.clientX, e.clientY);
    const g = this._pick();
    e.preventDefault();
    if (g) { this._scale(g, e.deltaY < 0 ? 1.1 : 1 / 1.1); this.select(g); }
    else this._dolly(e.deltaY < 0 ? 0.9 : 1.1);
  }

  _scale(g, f) { g.scale.multiplyScalar(f); g.scale.clampScalar(0.1, 12); }

  _dolly(f) {
    const dir = new THREE.Vector3().subVectors(this.camera.position, this.controls.target).multiplyScalar(f);
    if (dir.length() > 1.2 && dir.length() < 60) this.camera.position.copy(this.controls.target).add(dir);
  }

  /* ---------------------------------------------------------------- actions ---- */
  addObject(type) {
    const o = this._add(makeHoloObject(type));
    o.position.set((Math.random() - 0.5) * 3, 0.6, (Math.random() - 0.5) * 2);
    this.select(o);
    return o;
  }

  clone() {
    const src = this.selected || this.objects[this.objects.length - 1];
    if (!src) return null;
    const c = this._add(makeHoloObject(src.userData.type, src.userData.color));
    c.position.copy(src.position).add(new THREE.Vector3(1.2, 0, 0));
    c.scale.copy(src.scale); c.quaternion.copy(src.quaternion);
    this.select(c);
    return c;
  }

  deleteSelected() {
    const g = this.selected;
    if (!g) return false;
    this.world.remove(g);
    this.objects = this.objects.filter((o) => o !== g);
    g.traverse((n) => { if (n.geometry) n.geometry.dispose(); if (n.material) n.material.dispose(); });
    this.selected = null;
    return true;
  }

  /* ---------------------------------------------------------- save / export ---- */
  serialize() {
    return {
      version: 1,
      savedAt: new Date().toISOString(),
      camera: { position: this.camera.position.toArray(), target: this.controls.target.toArray() },
      objects: this.objects.map((o) => ({
        type: o.userData.type, color: o.userData.color,
        position: o.position.toArray(), rotation: [o.rotation.x, o.rotation.y, o.rotation.z],
        scale: o.scale.toArray(),
      })),
    };
  }

  load(project) {
    if (!project || !Array.isArray(project.objects)) return;
    this.select(null);
    [...this.objects].forEach((o) => { this.selected = o; this.deleteSelected(); });
    project.objects.slice(0, 2000).forEach((d) => {
      const o = this._add(makeHoloObject(d.type, d.color));
      if (Array.isArray(d.position)) o.position.fromArray(d.position);
      if (Array.isArray(d.rotation)) o.rotation.set(d.rotation[0], d.rotation[1], d.rotation[2]);
      if (Array.isArray(d.scale)) o.scale.fromArray(d.scale);
    });
    if (project.camera) {
      if (Array.isArray(project.camera.position)) this.camera.position.fromArray(project.camera.position);
      if (Array.isArray(project.camera.target)) this.controls.target.fromArray(project.camera.target);
    }
  }

  async exportGLB() {
    const { GLTFExporter } = await import("three/addons/exporters/GLTFExporter.js");
    return new GLTFExporter().parseAsync(this.world, { binary: true });   // ArrayBuffer
  }

  /* ------------------------------------------------------- neural mesh view ---- */
  showNeural(data) {
    const pos = data.positions || [], act = data.activity || [], mod = data.module || [];
    if (!pos.length) return;
    const n = pos.length, positions = new Float32Array(n * 3), colors = new Float32Array(n * 3);
    const c = new THREE.Color();
    for (let i = 0; i < n; i++) {
      // the mesh lives in a unit cube; place it as a 4-unit hologram above the floor
      positions[i * 3] = (pos[i][0] - 0.5) * 4;
      positions[i * 3 + 1] = pos[i][2] * 4 - 0.6;
      positions[i * 3 + 2] = (pos[i][1] - 0.5) * 4;
      c.setHex(MODULE_COLORS[(mod[i] || 0) % MODULE_COLORS.length]);
      const glow = 0.35 + Math.min(1, Math.abs(act[i] || 0) * 6);
      colors[i * 3] = c.r * glow; colors[i * 3 + 1] = c.g * glow; colors[i * 3 + 2] = c.b * glow;
    }
    if (!this.neural) {
      const geom = new THREE.BufferGeometry();
      const mat = new THREE.PointsMaterial({ size: 0.045, vertexColors: true, transparent: true,
                                             opacity: 0.9, depthWrite: false, blending: THREE.AdditiveBlending });
      this.neural = new THREE.Points(geom, mat);
      this.neural.name = "neural-mesh";
      this.scene.add(this.neural);
    }
    this.neural.geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    this.neural.geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    this.neural.geometry.computeBoundingSphere();
    this.neural.visible = true;
  }

  hideNeural() { if (this.neural) this.neural.visible = false; }
  neuralVisible() { return !!(this.neural && this.neural.visible); }

  /* -------------------------------------------------------- gesture control --- */
  gesturesOn() { return !!(this.gestures && this.gestures.enabled); }

  async toggleGesture() {
    if (!this.gestures) {
      try {
        const mod = await import("./gestures.js");
        this.gestures = new mod.Gestures({
          onPinch: (nx, ny, pinching) => this._gesturePinch(nx, ny, pinching),
          onTwoHand: (factor) => this._gestureScale(factor),
        });
      } catch (e) { console.error("gestures unavailable:", e); return false; }
    }
    return this.gestures.toggle();
  }

  _gesturePinch(nx, ny, pinching) {
    const r = this.renderer.domElement.getBoundingClientRect();
    this._setNdc(r.left + nx * r.width, r.top + ny * r.height);
    if (pinching && !this.dragging) { const g = this._pick(); if (g) { this.select(g); this._beginDrag(g); } }
    else if (pinching && this.dragging) this._dragTo();
    else if (!pinching && this.dragging) this._onUp();
  }

  _gestureScale(factor) {
    const g = this.selected;
    if (g) this._scale(g, factor); else this._dolly(1 / factor);
  }

  /* ---------------------------------------------------------- wallpaper mode -- */
  setWallpaper(on) {
    this.wallpaper = on;
    this.controls.enabled = this.interactive && !on;
    applyWallpaperVisual(this.renderer, this.scene, on);
  }

  /* -------------------------------------------------------------- lifecycle ---- */
  resize() {
    const w = this.container.clientWidth || window.innerWidth;
    const h = this.container.clientHeight || window.innerHeight;
    this.camera.aspect = w / h; this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h);
  }

  open() { if (this.running) return; this.running = true; this.resize(); this._loop(); }

  close() { this.running = false; if (this.gestures) this.gestures.disable(); }

  _loop() {
    if (!this.running) return;
    requestAnimationFrame(() => this._loop());
    this.objects.forEach((o, i) => { if (!(this.dragging && o === this.selected)) o.rotation.y += 0.002 + i * 0.0003; });
    if (this.neural && this.neural.visible) this.neural.rotation.y += 0.0015;
    if (this.autoOrbit) {
      const t = performance.now() * 0.00005;
      const r = Math.hypot(this.camera.position.x - this.controls.target.x, this.camera.position.z - this.controls.target.z) || 7;
      this.camera.position.x = this.controls.target.x + Math.sin(t) * r;
      this.camera.position.z = this.controls.target.z + Math.cos(t) * r;
      this.camera.lookAt(this.controls.target);
    } else {
      this.controls.update();
    }
    this.renderer.render(this.scene, this.camera);
  }

  dispose() {
    this.close();
    window.removeEventListener("resize", this._onResize);
    if (this.renderer) { this.renderer.dispose(); this.renderer.domElement.remove(); }
  }
}
