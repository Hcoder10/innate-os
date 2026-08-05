// Droppable props in the browser: one class covering both kinds the world
// server serves (see mars_sim_driver/props.py).
//
// The server sends a roster once per observer connection -- name, label, and
// how to draw each prop -- so adding a prop is a sidecar in sim/props/ and
// nothing here. Two ways a prop gets a body:
//
//   - a glb named by its sidecar's `viewer.glb`, normalized into the SAME
//     local frame its MuJoCo body uses so one pose quaternion orients both;
//   - otherwise (no glb, or the asset bundle never shipped it) the same
//     primitive physics is using, built from `collision`/`size`/`rgba`.
//
// The second is not a degraded path to apologise for: most props ARE
// primitives, and a prop whose mesh is missing still has to be visible or the
// 3D view disagrees with what the robot's cameras see.

import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";

/** How the browser should place a prop's glb into its MuJoCo body frame. */
export interface PropViewerDef {
  glb?: string;
  /** Standard glTF Y-up -> scene Z-up. False for a model already authored Z-up. */
  rotateToZUp?: boolean;
  /** Rescale so fitDim spans this many metres. */
  fitSizeM?: number;
  /** "height" = up-axis extent; "max" = largest bbox side. */
  fitDim?: "height" | "max";
  /** Where the body origin sits: feet-down vs geometric centre. */
  origin?: "base" | "center";
  /** CoACD hull soup (float32 xyz) in the body frame, for the collision overlay. */
  hulls?: string;
}

/** One prop as the world server describes it (props.py Prop.manifest). */
export interface PropInfo {
  name: string;
  label: string;
  title: string;
  /** Props laid out together by one click (props.py `group`). */
  group: string;
  collision: string;
  size: number[];
  rgba: number[];
  viewer: PropViewerDef;
}

/** Build the primitive MuJoCo is colliding with. Mirrors props.py's
 * _PRIMITIVE_FOR_SIZE: "hull"/"pieces" name a mesh rather than a shape, so a
 * prop whose mesh is absent falls back on what its `size` implies. */
function primitiveGeometry(info: PropInfo): THREE.BufferGeometry {
  const s = info.size;
  let shape = info.collision;
  if (shape !== "box" && shape !== "sphere" && shape !== "cylinder") {
    shape = s.length === 1 ? "sphere" : s.length === 2 ? "cylinder" : "box";
  }
  if (shape === "sphere") {
    // Finely tessellated: a coarse sphere casts a visibly polygonal shadow
    // right where it meets the floor, which is where the eye checks contact.
    return new THREE.SphereGeometry(s[0], 32, 24);
  }
  if (shape === "cylinder") {
    // MuJoCo sizes a cylinder (radius, half-length) and stands it on +z;
    // THREE's is (radius, radius, length) and Y-up.
    return new THREE.CylinderGeometry(s[0], s[0], s[1] * 2, 40).rotateX(Math.PI / 2);
  }
  // MuJoCo box sizes are half-extents.
  return new THREE.BoxGeometry(s[0] * 2, s[1] * 2, s[2] * 2);
}

/** Rescale + re-origin a raw glb into its MuJoCo body's local frame (glb
 * exports bake arbitrary origins, orientations and unit scales). */
function normalizeModel(scene: THREE.Object3D, def: PropViewerDef): void {
  if (def.rotateToZUp !== false) scene.rotation.x = Math.PI / 2; // glTF Y-up -> scene Z-up
  scene.updateMatrixWorld(true);
  const size = new THREE.Box3().setFromObject(scene).getSize(new THREE.Vector3());
  const upExtent = def.rotateToZUp !== false ? size.z : size.y;
  const span = def.fitDim === "height" ? upExtent : Math.max(size.x, size.y, size.z);
  if (def.fitSizeM && span > 0) scene.scale.multiplyScalar(def.fitSizeM / span);

  // Re-measure post-scale to place the origin.
  scene.updateMatrixWorld(true);
  const scaled = new THREE.Box3().setFromObject(scene);
  const center = scaled.getCenter(new THREE.Vector3());
  if (def.origin !== "base") {
    scene.position.sub(center);
    return;
  }
  // base: centred in the ground plane, up-axis min sitting at the origin.
  scene.position.x -= center.x;
  if (def.rotateToZUp !== false) {
    scene.position.y -= center.y;
    scene.position.z -= scaled.min.z;
  } else {
    scene.position.z -= center.z;
    scene.position.y -= scaled.min.y;
  }
}

/**
 * Every prop's body in the scene, driven by ground-truth poses.
 *
 * Each prop's root is built on first sight and then reused: a prop that is
 * taken away and put back down reappears rather than staying invisible.
 */
export class PropLibrary {
  /** Roster from the server, by name. Empty until the first manifest lands. */
  private info = new Map<string, PropInfo>();
  private roots = new Map<string, THREE.Group>();
  private loading = new Set<string>();
  private hulls: THREE.Mesh[] = [];
  private hullsVisible = false;

  constructor(
    private scene: THREE.Scene,
    private material: THREE.MeshBasicMaterial,
    /** Called when a prop's body first enters the scene (shadow box refit). */
    private onChanged: () => void = () => {},
  ) {}

  /** Adopt the server's roster. Props that vanish from it lose their bodies. */
  setManifest(props: PropInfo[]): void {
    this.info = new Map(props.map((p) => [p.name, p]));
    for (const [name, root] of this.roots) {
      if (!this.info.has(name)) {
        this.scene.remove(root);
        this.roots.delete(name);
      }
    }
  }

  get manifest(): PropInfo[] {
    return [...this.info.values()];
  }

  /** Mirror ground truth: {name: [x, y, z, qw, qx, qy, qz]}. A prop the block
   * stops naming has left the world (parked, or never dropped) and is hidden
   * rather than left behind at its last pose. */
  setPoses(poses: Record<string, number[]>): void {
    for (const [name, root] of this.roots) {
      if (!poses[name]) root.visible = false;
    }
    for (const [name, pose] of Object.entries(poses)) {
      const root = this.ensure(name);
      if (!root) continue; // unknown prop, or its glb is still loading
      // Explicitly, not just on creation: a prop that was removed and put down
      // again reuses its hidden root and would otherwise stay invisible.
      root.visible = true;
      root.position.set(pose[0], pose[1], pose[2]);
      root.quaternion.set(pose[4], pose[5], pose[6], pose[3]);
    }
  }

  setHullsVisible(visible: boolean): void {
    this.hullsVisible = visible;
    for (const hull of this.hulls) hull.visible = visible;
  }

  /** Every prop body currently in the world (shadow-box fitting). */
  get visibleRoots(): THREE.Object3D[] {
    return [...this.roots.values()].filter((r) => r.visible);
  }

  private ensure(name: string): THREE.Group | undefined {
    const existing = this.roots.get(name);
    if (existing) return existing;
    const info = this.info.get(name);
    if (!info) return undefined; // a prop this build was never told about

    const root = new THREE.Group();
    this.roots.set(name, root);
    this.scene.add(root);
    if (info.viewer.hulls) void this.loadHullSoup(info, root);

    if (!info.viewer.glb) {
      root.add(this.primitiveMesh(info));
      this.onChanged();
      return root;
    }
    // Show the primitive immediately, then swap in the glb once it arrives --
    // so a prop is never invisible, whether the mesh is slow or absent.
    const placeholder = this.primitiveMesh(info);
    root.add(placeholder);
    if (!this.loading.has(name)) {
      this.loading.add(name);
      void this.loadGlb(info, root, placeholder);
    }
    this.onChanged();
    return root;
  }

  private primitiveMesh(info: PropInfo): THREE.Mesh {
    const [r, g, b] = info.rgba;
    const mesh = new THREE.Mesh(
      primitiveGeometry(info),
      new THREE.MeshStandardMaterial({ color: new THREE.Color(r, g, b), roughness: 0.7 }),
    );
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    return mesh;
  }

  private async loadGlb(info: PropInfo, root: THREE.Group, placeholder: THREE.Mesh): Promise<void> {
    try {
      const gltf = await new GLTFLoader().loadAsync(info.viewer.glb!);
      normalizeModel(gltf.scene, info.viewer);
      gltf.scene.traverse((obj) => {
        if (obj instanceof THREE.Mesh) {
          obj.castShadow = true;
          obj.receiveShadow = true;
        }
      });
      root.remove(placeholder);
      placeholder.geometry.dispose();
      root.add(gltf.scene);
      this.onChanged();
    } catch (err) {
      // Expected whenever the asset bundle predates this prop: keep the
      // primitive, which is what physics is using anyway.
      console.warn(`[sim-viewer] prop '${info.name}' has no model (${info.viewer.glb}); drawing its primitive`, err);
    }
  }

  private async loadHullSoup(info: PropInfo, root: THREE.Group): Promise<void> {
    try {
      const res = await fetch(info.viewer.hulls!);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(await res.arrayBuffer()), 3));
      const hull = new THREE.Mesh(geometry, this.material);
      hull.visible = this.hullsVisible; // honour a toggle made before the drop
      this.hulls.push(hull);
      root.add(hull);
    } catch (err) {
      console.warn(`[sim-viewer] collision soup missing for '${info.name}'; overlay skipped`, err);
    }
  }
}
