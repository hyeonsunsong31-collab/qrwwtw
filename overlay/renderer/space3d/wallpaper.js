/* space3d/wallpaper.js — "3D space replaces the desktop wallpaper" (requirement 18).
 *
 * Two layers cooperate:
 *   1. This renderer: when wallpaper mode is on we deepen the background and stop the
 *      scene from capturing the mouse, so it behaves like a live wallpaper.
 *   2. The main process (overlay/main.js, ipc 'space3d:wallpaper'): sends the window
 *      below other windows / to the desktop layer.
 *
 * TRUE "behind the desktop icons" on Windows uses the WorkerW window that Explorer owns
 * (the same trick Wallpaper Engine uses): find Progman, send it 0x052C to spawn a WorkerW,
 * then SetParent(ourHwnd, WorkerW). That requires a tiny native call (user32) and is wired
 * from boot/jarvis_service.py on Windows when `space3d.replace_wallpaper` is enabled. When
 * that native step isn't available, the overlay still covers the desktop as a full-screen
 * bottom layer, which visually replaces the wallpaper with the live 3D scene.
 */
import * as THREE from "three";

export function applyWallpaperVisual(renderer, scene, on) {
  if (!renderer) return;
  if (on) {
    renderer.setClearColor(0x01080f, 1);            // opaque deep void behind icons
    if (scene) scene.fog = new THREE.FogExp2(0x01080f, 0.05);
  } else {
    renderer.setClearColor(0x000000, 0);            // transparent again (overlay mode)
    if (scene) scene.fog = new THREE.FogExp2(0x03121b, 0.035);
  }
}
