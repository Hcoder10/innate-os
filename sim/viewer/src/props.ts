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
//
// Models come from PropModels, which parses them ahead of any drop; this file
// only decides how long to wait for one.

import * as THREE from "three";
import { PropModels } from "./propModels";

/** How long a prop with a glb stays undrawn waiting for its model before we
 * settle for the primitive. A prefetched model is ready well inside this, so
 * the window only opens for a drop that beats its own prefetch. */
const MODEL_GRACE_MS = 300;

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
  /** Props laid out together by one click, or null for a prop that is only
   * ever placed deliberately (props.py `group`). */
  group: string | null;
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
  /** When each prop was first seen in a pose block, for MODEL_GRACE_MS. */
  private firstSeen = new Map<string, number>();
  private models: PropModels;
  private prefetching = false;
  private hulls: THREE.Mesh[] = [];
  private hullsVisible = false;

  constructor(
    private scene: THREE.Scene,
    private material: THREE.MeshBasicMaterial,
    /** Called when a prop's body first enters the scene (shadow box refit). */
    private onChanged: () => void = () => {},
    /** Called with each model as it finishes parsing, to warm its textures. */
    onModelReady: (model: THREE.Group) => void = () => {},
  ) {
    this.models = new PropModels(onModelReady);
  }

  /** Adopt the server's roster. Props that vanish from it lose their bodies. */
  setManifest(props: PropInfo[]): void {
    this.info = new Map(props.map((p) => [p.name, p]));
    for (const [name, root] of this.roots) {
      if (!this.info.has(name)) {
        this.scene.remove(root);
        this.roots.delete(name);
        this.firstSeen.delete(name); // re-added later, it gets a fresh grace window
      }
    }
    if (this.prefetching) this.prefetchModels();
  }

  /**
   * Parse every prop's glb now, so a later drop can use it on the frame it
   * lands. Called once the robot and apartment are done, so prop models never
   * compete with them for the load queue's bandwidth.
   *
   * Safe to call before the roster arrives: it latches, and setManifest runs
   * it again once there is something to prefetch.
   */
  prefetchModels(): void {
    this.prefetching = true;
    this.models.prefetch(this.info.values());
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

    // Hold a prop with a glb undrawn for up to MODEL_GRACE_MS rather than
    // showing a primitive we are about to replace. setPoses retries every
    // frame, so the prop appears as soon as either the model or the grace
    // window is done -- and at once if we already know no model is coming.
    const waiting = info.viewer.glb && !this.models.get(name) && !this.models.hasFailed(name);
    if (waiting && !this.graceExpired(name, info)) return undefined;

    const root = new THREE.Group();
    this.roots.set(name, root);
    this.scene.add(root);
    if (info.viewer.hulls) void this.loadHullSoup(info, root);

    const model = this.models.get(name);
    if (model) {
      root.add(model);
    } else {
      // No glb, or one still parsing past its grace window: draw the primitive
      // physics is using, and swap the model in if it does still arrive.
      const placeholder = this.primitiveMesh(info);
      root.add(placeholder);
      if (info.viewer.glb) void this.swapWhenReady(info, root, placeholder);
    }
    this.onChanged();
    return root;
  }

  /** Start this prop's model load on first sight, then report whether it has
   * since waited out MODEL_GRACE_MS. */
  private graceExpired(name: string, info: PropInfo): boolean {
    const since = this.firstSeen.get(name);
    if (since === undefined) {
      this.firstSeen.set(name, performance.now());
      void this.models.load(info); // dropped before its prefetch ran: parse it now
      return false;
    }
    return performance.now() - since >= MODEL_GRACE_MS;
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

  /** Replace a prop's primitive once its model lands -- only reached when the
   * parse outran MODEL_GRACE_MS. */
  private async swapWhenReady(info: PropInfo, root: THREE.Group, placeholder: THREE.Mesh): Promise<void> {
    const model = await this.models.load(info);
    if (!model || placeholder.parent !== root) return; // failed, or the prop left the roster
    root.remove(placeholder);
    placeholder.geometry.dispose();
    root.add(model);
    this.onChanged();
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
