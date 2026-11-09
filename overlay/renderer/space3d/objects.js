/* space3d/objects.js -- holographic object factory (requirement 5).
 * Each object is a translucent emissive solid plus a bright wireframe overlay, so it reads
 * as an Iron-Man-style hologram. Objects carry userData for selection/drag/scale/clone/save. */
import * as THREE from "three";

const PALETTE = [0x38e1ff, 0x61ffd0, 0x8af3ff, 0x5ea2ff, 0xa0ffe8];

const GEOMS = {
  box: () => new THREE.BoxGeometry(1, 1, 1),
  sphere: () => new THREE.SphereGeometry(0.7, 32, 24),
  torus: () => new THREE.TorusGeometry(0.6, 0.22, 20, 48),
  cone: () => new THREE.ConeGeometry(0.6, 1.2, 28),
  cylinder: () => new THREE.CylinderGeometry(0.55, 0.55, 1.1, 32),
  icosa: () => new THREE.IcosahedronGeometry(0.75, 0),
};
export const OBJECT_TYPES = Object.keys(GEOMS);

export function makeHoloObject(type, color) {
  type = GEOMS[type] ? type : OBJECT_TYPES[Math.floor(Math.random() * OBJECT_TYPES.length)];
  color = (typeof color === "number") ? color : PALETTE[Math.floor(Math.random() * PALETTE.length)];
  const geom = GEOMS[type]();

  const solid = new THREE.Mesh(geom, new THREE.MeshStandardMaterial({
    color, emissive: color, emissiveIntensity: 0.35, metalness: 0.2, roughness: 0.35,
    transparent: true, opacity: 0.45,
  }));
  const edges = new THREE.LineSegments(
    new THREE.EdgesGeometry(geom),
    new THREE.LineBasicMaterial({ color: 0xbfffff, transparent: true, opacity: 0.9 }));

  const group = new THREE.Group();
  group.name = `holo-${type}`;
  group.add(solid); group.add(edges);
  group.userData = { selectable: true, type, color, solid, edges };
  return group;
}

export function setSelected(group, on) {
  if (!group || !group.userData || !group.userData.solid) return;
  group.userData.solid.material.emissiveIntensity = on ? 0.9 : 0.35;
  group.userData.edges.material.color.set(on ? 0xffffff : 0xbfffff);
}
