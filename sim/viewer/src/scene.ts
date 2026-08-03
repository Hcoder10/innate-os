// SimScene — Three.js scene: the apartment.glb environment (visual only)
// plus the real MARS robot from its ROS URDF. Convention: Z-up, X-forward
// (REP-103); the URDF loads unrotated, the Y-up glb is rotated on load.

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OBJLoader } from "three/addons/loaders/OBJLoader.js";
import URDFLoader from "urdf-loader";
import type { URDFRobot } from "urdf-loader";

const APARTMENT_URL = "/models/appartement.glb";
const ROBOT_URDF_URL = "/robot/mars.urdf";

// The manipulation props (world.py GRASP_OBJECTS). They are MuJoCo primitives,
// so the browser rebuilds them here rather than loading a mesh -- keep the
// dimensions and colours in step with world.py. THREE's cylinder is Y-up while
// MuJoCo's is Z-up, hence the rotated geometry.
const GRASP_OBJECT_SHAPES: Record<string, { geometry: () => THREE.BufferGeometry; color: number }> = {
  cube: { geometry: () => new THREE.BoxGeometry(0.04, 0.04, 0.04), color: 0xd94739 },
  sock: { geometry: () => new THREE.BoxGeometry(0.04, 0.04, 0.06), color: 0x73757f },
  can: {
    geometry: () => new THREE.CylinderGeometry(0.02, 0.02, 0.06, 40).rotateX(Math.PI / 2),
    color: 0x408cd9,
  },
  bar: { geometry: () => new THREE.BoxGeometry(0.03, 0.1, 0.03), color: 0xcc541a },
  // Finely tessellated: a coarse sphere casts a visibly polygonal shadow right
  // where it meets the floor, which is the one place the eye checks for contact.
  ball: { geometry: () => new THREE.SphereGeometry(0.0225, 32, 24), color: 0x66cc73 },
};

// These URDF links carry no real body geometry — just small marker spheres
// used to visualize the end-effector / camera optical frames (e.g. in
// RViz). Hide them here rather than in the URDF itself, since that file is
// shared with the rest of the ROS stack.
const HIDDEN_FRAME_LINKS: string[] = ["ee_link", "head_camera_left", "head_camera_right"];

// Arm links painted orange. Every URDF link actually shares the matt_black
// material, so we override by link name rather than by material.
const ORANGE_LINKS = new Set(["link1", "link3", "link5"]);

// Key light offset from whatever it is lighting; setPose slides it along with
// the robot so the tight shadow box stays centred on it. The direction is the
// original key light's, so the robot shades the same as it always did.
const KEY_LIGHT_OFFSET: [number, number, number] = [2.0, -1.5, 3.0];

// The shadow box covers the robot AND every prop in the world, so nothing
// loses its shadow by being left behind -- but it shrinks to the minimum that
// does, because texel size is 2 * box / map and that is what decides whether
// shadows look sharp or blocky. 0.7m is the floor (a robot 0.35m across with
// a 0.36m reach, props dropped within 0.35m): 0.68mm per texel at 2048. Past
// SHADOW_BOX_MAX_M it stops growing and distant props lose their shadow
// rather than blurring the robot's, which is the part being looked at.
const SHADOW_BOX_MIN_M = 0.7;
const SHADOW_BOX_MAX_M = 3.0;
const SHADOW_BOX_STEP_M = 0.25; // quantised, so the box does not resize every frame
const SHADOW_MARGIN_M = 0.5; // the robot's own extent plus the throw of its shadow
const SHADOW_MAP_PX = 2048;

// Chase-cam framing used when a pose is snapped in (see spawnAt below).
const CHASE_DISTANCE = 1.8; // meters behind the robot
const CHASE_HEIGHT = 1.1; // meters above the ground

// Robot-mounted camera views: frames, axis conventions, FOV and near plane
// match the driver's cameras (mars_sim_driver.core's CAMERAS).
export type CameraView = "orbit" | "main" | "arm";
// Tracks mars_sim_driver/constants.py CAMERA_FOVY: the real head camera's
// focal length (fx ~= 355 @640x480), not a display preference.
const ROBOT_CAMERA_VFOV = 68.5;
// Don't shrink to fix the near-clipped gripper: the origin sits inside the
// wrist housing, so a smaller near renders the housing interior instead.
const ROBOT_CAMERA_NEAR = 0.03;
const ROBOT_CAMERA_MOUNTS: Array<{
  view: Exclude<CameraView, "orbit">;
  frame: string;
  forward: THREE.Vector3;
  up: THREE.Vector3;
}> = [
  { view: "main", frame: "camera_optical_frame", forward: new THREE.Vector3(0, 0, 1), up: new THREE.Vector3(0, -1, 0) },
  { view: "arm", frame: "arm_camera_link", forward: new THREE.Vector3(1, 0, 0), up: new THREE.Vector3(0, 0, 1) },
];

export class SimScene {
  readonly scene = new THREE.Scene();
  readonly camera: THREE.PerspectiveCamera;
  readonly renderer: THREE.WebGLRenderer;
  readonly controls: OrbitControls;

  followCamera = true;

  // Hidden until the first real pose (spawnAt): a robot at the world origin
  // before state arrives reads as "spawned in the wrong place" when the true
  // failure is "no state yet".
  private robotRoot = new THREE.Group();
  private robot?: URDFRobot;
  private followPrevXY: [number, number] = [0, 0];
  private glossyMaterialCache = new Map<THREE.Material, THREE.MeshStandardMaterial>();
  private orange?: THREE.MeshStandardMaterial;
  private robotCameras = new Map<CameraView, THREE.PerspectiveCamera>();
  private activeView: CameraView = "orbit";
  private lidarPoints?: THREE.Points;
  private keyLight?: THREE.DirectionalLight;
  private shadowCatcher?: THREE.Mesh;
  private shadowBoxM = SHADOW_BOX_MIN_M;
  private robotXY: [number, number] = [0, 0];
  private hullsGroup?: THREE.Group;
  private hullsPromise?: Promise<void>;
  private hullsVisible = false;
  // The URDF's <collision> subtrees, one per link. They hang off the link in
  // the robot's own graph, so they follow the joints for free.
  private robotColliders: THREE.Object3D[] = [];
  private hullMaterial = new THREE.MeshBasicMaterial({ color: 0x00ff88, wireframe: true });
  // One mesh per manipulation prop, built on the first state that names it.
  private objectMeshes = new Map<string, THREE.Mesh>();

  /** Fixed render size (offscreen use, e.g. SimSession); null = track the window. */
  private fixedSize: { width: number; height: number } | null = null;

  constructor(canvas: HTMLCanvasElement, opts: { fixedSize?: { width: number; height: number } } = {}) {
    this.fixedSize = opts.fixedSize ?? null;
    const w = this.fixedSize?.width ?? window.innerWidth;
    const h = this.fixedSize?.height ?? window.innerHeight;
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    this.renderer.setPixelRatio(this.fixedSize ? 1 : Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(w, h, !this.fixedSize);
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.5;

    this.scene.background = new THREE.Color(0x14161a);
    this.scene.fog = new THREE.FogExp2(0x14161a, 0.035);

    this.camera = new THREE.PerspectiveCamera(55, w / h, 0.05, 200);
    this.camera.up.set(0, 0, 1);
    this.camera.position.set(-3.5, -3.5, 2.4);

    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.target.set(0, 0, 0.4);
    this.controls.minDistance = 0.5;
    this.controls.maxDistance = 30;
    this.controls.update();

    this.addLights();
    this.addGround();
    this.addShadowCatcher();
    this.robotRoot.visible = false;
    this.scene.add(this.robotRoot);

    if (!this.fixedSize) window.addEventListener("resize", () => this.onResize());
  }

  private addLights(): void {
    // No env map on purpose: it grey-washes dark low-roughness materials;
    // several directional lights give the distinct highlights that read as
    // "glossy" on the robot parts.
    this.scene.add(new THREE.AmbientLight(0xffffff, 1.2));

    // The shadow map follows the robot (see setPose) over a 5m box rather than
    // trying to cover the whole flat. That is 2.4mm per texel, fine enough for
    // the arm's ~2cm detail and the manipulation props to cast real contact
    // shadows -- without one under the gripper there is no depth cue to judge
    // a grasp by. It also lets normalBias stay at 12mm; the 50mm it needed
    // over a 16m box erased the shadow of anything smaller than 50mm, i.e.
    // every part that matters here.
    const key = new THREE.DirectionalLight(0xffffff, 2.0);
    key.castShadow = true;
    key.shadow.mapSize.set(SHADOW_MAP_PX, SHADOW_MAP_PX);
    key.shadow.camera.near = 0.1;
    key.shadow.camera.far = 12;
    key.shadow.camera.left = -SHADOW_BOX_MIN_M;
    key.shadow.camera.right = SHADOW_BOX_MIN_M;
    key.shadow.camera.top = SHADOW_BOX_MIN_M;
    key.shadow.camera.bottom = -SHADOW_BOX_MIN_M;
    // Both biases stay near zero, and that is safe rather than sloppy.
    // Shadow bias exists to stop a surface shadowing ITSELF, and three renders
    // the flipped side into the shadow map (WebGLShadowMap's shadowSide: a
    // FrontSide material casts from its back faces), so the depth stored is
    // already the far side of every closed mesh. The shadow catcher casts
    // nothing at all, so it cannot self-shadow under any circumstances -- for
    // the floor shadow, every millimetre of bias is pure daylight between an
    // object and its own shadow, worst under a sphere or a cube corner. The
    // 0.5mm left is a margin for the robot's own meshes, which do receive.
    key.shadow.bias = 0;
    key.shadow.normalBias = 0.0005;
    // REQUIRED. DirectionalLightShadow builds its camera as
    // OrthographicCamera(-5, 5, 5, -5, 0.5, 500) and computes the projection
    // in that constructor; LightShadow.updateMatrices never recomputes it. So
    // every frustum value set above is ignored until this call, which is why
    // nothing cast a shadow: the box stayed the default 10m centred on the
    // world origin, while the robot spawns 4.3m away and drives off from
    // there. Delete this line and the shadows go away again.
    key.shadow.camera.updateProjectionMatrix();
    key.position.set(...KEY_LIGHT_OFFSET);
    this.scene.add(key);
    this.scene.add(key.target);
    this.keyLight = key;

    const fill = new THREE.DirectionalLight(0xaaccff, 0.8);
    fill.position.set(-4, 3, 3);
    this.scene.add(fill);

    this.scene.add(new THREE.HemisphereLight(0xaabbcc, 0x445566, 1.2));
  }

  /** A transparent plane under the robot that shows nothing but the shadows
   * falling on it.
   *
   * The apartment glb is baked: every one of its materials carries
   * KHR_materials_unlit, so GLTFLoader gives them MeshBasicMaterial, which
   * ignores lights entirely. The shading you see in the flat -- including the
   * shadow under the sofa -- is painted into the texture, and receiveShadow on
   * those meshes does nothing. Without this plane no shadow can ever land on
   * the floor, however the light is set up.
   *
   * It rides with the robot (see setPose) because it only needs to cover the
   * shadow box, and sits ON the physics floor at z=0 so shadows start where
   * the object touches. Where the visual floor is raised (a rug), the rug
   * draws over the shadow. */
  private addShadowCatcher(): void {
    const mesh = new THREE.Mesh(
      // Big enough to cover the widest shadow box; it costs nothing where no
      // shadow lands. PlaneGeometry is already XY / +Z normal, i.e. our floor.
      new THREE.PlaneGeometry(2 * SHADOW_BOX_MAX_M + 2, 2 * SHADOW_BOX_MAX_M + 2),
      // Coplanar with the floor at z=0 rather than lifted off it: a plane
      // floated even 4mm up starts the shadow 4mm up the object's side, which
      // at this light angle is a visible gap under everything. polygonOffset
      // wins the z-fight in depth instead, without moving it in world space.
      new THREE.ShadowMaterial({
        opacity: 0.42,
        depthWrite: false,
        polygonOffset: true,
        polygonOffsetFactor: -2,
        polygonOffsetUnits: -2,
      }),
    );
    mesh.receiveShadow = true;
    mesh.castShadow = false;
    mesh.renderOrder = 1;
    this.shadowCatcher = mesh;
    this.scene.add(mesh);
  }

  private addGround(): void {
    // GridHelper lies in the XZ plane by default; rotate it into our XY
    // (ground) plane. Faint — the apartment mesh is the real floor. Nudged
    // just below z=0 so it doesn't z-fight with the apartment floor mesh
    // (which sits right at z=0) and show through as stray lines.
    const grid = new THREE.GridHelper(40, 80, 0x2a2d33, 0x1c1e22);
    grid.rotation.x = Math.PI / 2;
    grid.position.z = -0.02;
    this.scene.add(grid);
  }

  /** Update the lidar overlay with world-frame hit points from /scan. */
  setLidarPoints(points: Float32Array): void {
    if (!this.lidarPoints) {
      const geometry = new THREE.BufferGeometry();
      const material = new THREE.PointsMaterial({ color: 0xff3333, size: 0.04 });
      this.lidarPoints = new THREE.Points(geometry, material);
      this.lidarPoints.visible = false;
      this.lidarPoints.frustumCulled = false;
      this.scene.add(this.lidarPoints);
    }
    this.lidarPoints.geometry.setAttribute("position", new THREE.BufferAttribute(points, 3));
  }

  setLidarVisible(visible: boolean): void {
    if (this.lidarPoints) this.lidarPoints.visible = visible;
  }

  /**
   * Wireframe overlay of everything the driver collides with: the robot's own
   * <collision> primitives from mars.urdf (already posed by the URDF graph,
   * so they track the joints) plus the apartment_collisions_v2 hull set
   * mars_sim_driver collides against. The hulls are lazily fetched on first
   * show (manifest.json lists the OBJs since a browser can't list a
   * directory) and rotated Y-up -> Z-up like the apartment glb.
   */
  setCollisionHullsVisible(visible: boolean): void {
    this.hullsVisible = visible;
    if (visible && !this.hullsPromise) {
      // ~1300 OBJ fetches; takes seconds on first show. A failure resets the
      // promise so toggling again retries instead of staying dead forever.
      this.hullsPromise = this.loadCollisionHulls().catch((err) => {
        console.error("[sim-viewer] collision hulls failed to load:", err);
        this.hullsPromise = undefined;
      });
    }
    if (this.hullsGroup) this.hullsGroup.visible = visible;
    for (const collider of this.robotColliders) collider.visible = visible;
  }

  private async loadCollisionHulls(): Promise<void> {
    const group = new THREE.Group();
    group.rotation.x = Math.PI / 2;
    const baseUrl = "/physics/apartment_collisions_v2/";
    const material = this.hullMaterial;

    // Fast path: one binary triangle soup (float32 xyz), one fetch, no
    // parsing -- publish_assets writes it next to the per-hull OBJs.
    const bin = await fetch(`${baseUrl}hulls.f32`);
    if (bin.ok) {
      const positions = new Float32Array(await bin.arrayBuffer());
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      group.add(new THREE.Mesh(geometry, material));
    } else {
      // Older bundles: fetch + parse every hull OBJ individually (slow).
      const manifest: string[] = await (await fetch(`${baseUrl}manifest.json`)).json();
      const loader = new OBJLoader();
      await Promise.all(
        manifest.map(async (filename) => {
          const obj = await loader.loadAsync(`${baseUrl}${filename}`);
          obj.traverse((child) => {
            if (child instanceof THREE.Mesh) child.material = material;
          });
          group.add(obj);
        }),
      );
    }
    group.visible = this.hullsVisible; // honor toggles made while loading
    this.hullsGroup = group;
    this.scene.add(group);
  }

  async loadApartment(): Promise<void> {
    const loader = new GLTFLoader();
    const gltf = await loader.loadAsync(APARTMENT_URL);
    const root = gltf.scene;
    root.rotation.x = Math.PI / 2; // glTF Y-up -> scene Z-up
    root.traverse((obj) => {
      if (obj instanceof THREE.Mesh) {
        // Does nothing, kept only so this doesn't look like an oversight: the
        // glb's materials are all KHR_materials_unlit, so GLTFLoader makes
        // them MeshBasicMaterial and they cannot receive a shadow. Their
        // shading is baked into the texture. addShadowCatcher is what puts
        // the robot's own shadow on the floor. castShadow is deliberately
        // left off -- the flat already shades itself.
        obj.receiveShadow = true;
        // Force FrontSide (the glb ships doubleSided): walls draw only from
        // inside the room, so overview cameras get the dollhouse-cutaway
        // look. If winding issues hide geometry, flip to BackSide.
        const setFrontSide = (mat: THREE.Material) => {
          mat.side = THREE.FrontSide;
        };
        if (Array.isArray(obj.material)) obj.material.forEach(setFrontSide);
        else setFrontSide(obj.material);
      }
    });
    this.scene.add(root);
  }

  async loadRobot(): Promise<URDFRobot> {
    // loadAsync resolves at URDF parse, but the STL meshes attach later via
    // this LoadingManager -- wait for onLoad or the restyle below misses them.
    const manager = new THREE.LoadingManager();
    const allMeshesLoaded = new Promise<void>((resolve) => {
      manager.onLoad = () => resolve();
    });

    const loader = new URDFLoader(manager);
    loader.packages = { mars_sim: "/robot" };
    loader.parseCollision = true; // the collisions overlay draws these
    const robot = await loader.loadAsync(ROBOT_URDF_URL);
    await allMeshesLoaded;

    for (const name of HIDDEN_FRAME_LINKS) {
      const link = robot.links[name];
      if (link) link.visible = false;
    }

    // Collider subtrees first, so the visual restyle below can skip them.
    robot.traverse((obj) => {
      if (!(obj as { isURDFCollider?: boolean }).isURDFCollider) return;
      obj.visible = this.hullsVisible;
      this.robotColliders.push(obj);
      obj.traverse((child) => {
        child.userData.collider = true;
        if (child instanceof THREE.Mesh) child.material = this.hullMaterial;
      });
    });

    robot.traverse((obj) => {
      if (obj.userData.collider) return;
      if (obj instanceof THREE.Mesh) {
        obj.castShadow = true;
        obj.receiveShadow = true;
        const linkName = nearestLinkName(obj);
        if (linkName && ORANGE_LINKS.has(linkName)) {
          obj.material = this.orangeMaterial();
        } else {
          obj.material = Array.isArray(obj.material)
            ? obj.material.map((m) => this.toGlossyMaterial(m))
            : this.toGlossyMaterial(obj.material);
        }
      }
    });
    this.robotRoot.add(robot);
    this.robot = robot;

    for (const mount of ROBOT_CAMERA_MOUNTS) {
      const frame = robot.frames[mount.frame];
      if (!frame) {
        console.warn(`[scene] camera frame "${mount.frame}" not found in URDF -- "${mount.view}" view unavailable`);
        continue;
      }
      const cam = new THREE.PerspectiveCamera(
        ROBOT_CAMERA_VFOV,
        this.viewSize().width / this.viewSize().height,
        ROBOT_CAMERA_NEAR,
        100,
      );
      // three.js cameras look down their local -Z with +Y up; build that
      // basis from the mount's forward/up convention.
      const zAxis = mount.forward.clone().negate();
      const xAxis = mount.up.clone().cross(zAxis).normalize();
      cam.quaternion.setFromRotationMatrix(new THREE.Matrix4().makeBasis(xAxis, mount.up.clone(), zAxis));
      frame.add(cam);
      this.robotCameras.set(mount.view, cam);
    }
    return robot;
  }

  /** Fit the shadow box around the robot AND every prop in the world, then
   * park the light and the catcher on it.
   *
   * Pinned to the robot, anything left behind stops having a shadow the moment
   * it leaves the box, which just reads as a bug. Sized to the whole set
   * always, one prop dropped across the room costs sharpness everywhere. So it
   * tracks the actual spread, quantised so it is not resized every frame, and
   * stops growing at SHADOW_BOX_MAX_M -- past that a distant prop loses its
   * shadow rather than blurring the robot's, which is the part being looked
   * at. normalBias follows the texel size, since its whole job is to clear
   * about one texel. */
  private updateShadowVolume(): void {
    const key = this.keyLight;
    if (!key) return;

    // Props further than the box could ever reach are dropped rather than
    // dragging the centre off the robot: at the cap, a midpoint between the
    // two would push the robot itself out of its own shadow box.
    const reach = SHADOW_BOX_MAX_M - SHADOW_MARGIN_M;
    const points: Array<[number, number]> = [this.robotXY];
    for (const mesh of this.objectMeshes.values()) {
      if (!mesh.visible) continue;
      const dx = mesh.position.x - this.robotXY[0];
      const dy = mesh.position.y - this.robotXY[1];
      if (Math.hypot(dx, dy) <= reach) points.push([mesh.position.x, mesh.position.y]);
    }
    const xs = points.map((pt) => pt[0]);
    const ys = points.map((pt) => pt[1]);
    const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
    const cy = (Math.min(...ys) + Math.max(...ys)) / 2;
    // Radius, not half-width: the box is axis-aligned in the LIGHT's frame,
    // not the world's. SHADOW_MARGIN_M covers the robot's own extent and the
    // throw of its shadow beyond whichever point is furthest out.
    const needed = Math.max(...points.map((pt) => Math.hypot(pt[0] - cx, pt[1] - cy))) + SHADOW_MARGIN_M;
    const box = Math.min(
      SHADOW_BOX_MAX_M,
      Math.max(SHADOW_BOX_MIN_M, Math.ceil(needed / SHADOW_BOX_STEP_M) * SHADOW_BOX_STEP_M),
    );
    if (box !== this.shadowBoxM) {
      this.shadowBoxM = box;
      key.shadow.camera.left = -box;
      key.shadow.camera.right = box;
      key.shadow.camera.top = box;
      key.shadow.camera.bottom = -box;
      key.shadow.camera.updateProjectionMatrix(); // never automatic; see addLights
      key.shadow.normalBias = Math.max(0.0005, ((2 * box) / SHADOW_MAP_PX) * 0.8);
    }

    // Snap to whole texels: slid continuously the map re-samples every frame
    // and the shadow edges crawl, which reads as cheap however sharp they are.
    const texel = (2 * box) / SHADOW_MAP_PX;
    const sx = Math.round(cx / texel) * texel;
    const sy = Math.round(cy / texel) * texel;
    key.position.set(sx + KEY_LIGHT_OFFSET[0], sy + KEY_LIGHT_OFFSET[1], KEY_LIGHT_OFFSET[2]);
    key.target.position.set(sx, sy, 0);
    key.target.updateMatrixWorld();
    this.shadowCatcher?.position.set(sx, sy, 0);
  }

  /** Views actually available (a frame can be missing from the URDF). */
  get availableViews(): CameraView[] {
    return ["orbit", ...this.robotCameras.keys()];
  }

  /** Switch what render() draws: the orbit camera or a robot-mounted one.
   * Falls back to orbit if the requested view's frame wasn't in the URDF. */
  setView(view: CameraView): void {
    this.activeView = view === "orbit" || this.robotCameras.has(view) ? view : "orbit";
    this.controls.enabled = this.activeView === "orbit";
  }

  /** Drive the URDF's arm/head joints to match physics-simulated angles (radians). */
  setJointAngles(joints: Record<string, number>): void {
    this.robot?.setJointValues(joints);
  }

  /** Mirror the manipulation props from ground truth, keyed by name
   * ({name: [x, y, z, qw, qx, qy, qz]} -- world_server's "objects" block).
   * Each mesh is built on first sight from GRASP_OBJECT_SHAPES; a prop the
   * block stops naming has left the world (parked, or never dropped) and is
   * hidden rather than left behind at its last pose. */
  setObjectPoses(poses: Record<string, number[]>): void {
    for (const [name, mesh] of this.objectMeshes) {
      if (!poses[name]) mesh.visible = false;
    }
    for (const [name, pose] of Object.entries(poses)) {
      let mesh = this.objectMeshes.get(name);
      if (!mesh) {
        const shape = GRASP_OBJECT_SHAPES[name];
        if (!shape) continue; // a prop this build doesn't know how to draw
        mesh = new THREE.Mesh(shape.geometry(), new THREE.MeshStandardMaterial({ color: shape.color, roughness: 0.7 }));
        mesh.castShadow = true;
        mesh.receiveShadow = true;
        this.objectMeshes.set(name, mesh);
        this.scene.add(mesh);
      }
      // Explicitly, not just on creation: a prop that was removed and dropped
      // again reuses its hidden mesh, and would otherwise stay invisible until
      // a page reload rebuilt it.
      mesh.visible = true;
      mesh.position.set(pose[0], pose[1], pose[2]);
      mesh.quaternion.set(pose[4], pose[5], pose[6], pose[3]);
    }
    this.updateShadowVolume(); // the props moved; the box may need to grow or shrink
  }

  // Orange accent for the arm links (see ORANGE_LINKS). Cached so every mesh
  // on those links shares one material.
  private orangeMaterial(): THREE.MeshStandardMaterial {
    if (!this.orange) {
      this.orange = new THREE.MeshStandardMaterial({
        color: new THREE.Color(1.0, 0.5, 0.0),
        metalness: 0.4,
        roughness: 0.5,
        side: THREE.DoubleSide, // keeps near-clipped shells solid in the wrist cam
      });
    }
    return this.orange;
  }

  // Swap the URDF's flat MeshPhong for PBR: moderate metalness for a soft
  // sheen (pure metal with no env map reads near-black), rough enough to
  // stay matte.
  private toGlossyMaterial(source: THREE.Material): THREE.MeshStandardMaterial {
    const cached = this.glossyMaterialCache.get(source);
    if (cached) return cached;

    const color = source instanceof THREE.MeshPhongMaterial ? source.color.clone() : new THREE.Color(0xffffff);
    const isDark = (color.r + color.g + color.b) / 3 < 0.4;
    if (isDark) {
      // matt_black is 0.05 — a near-black void once shaded. Lift slightly
      // toward a dark charcoal so the robot's form reads from every angle
      // while staying clearly dark (not full black).
      color.lerp(new THREE.Color(0.12, 0.12, 0.12), 0.4);
    }
    const material = new THREE.MeshStandardMaterial({
      color,
      metalness: isDark ? 0.45 : 0.4,
      roughness: isDark ? 0.5 : 0.55,
      side: THREE.DoubleSide, // see orangeMaterial
    });
    this.glossyMaterialCache.set(source, material);
    return material;
  }

  /**
   * Place the robot at a pose immediately and frame the camera zoomed in
   * behind it (chase-cam), rather than following the incremental delta used
   * by setPose. Use once — e.g. on spawn or reset — before driving resumes.
   */
  spawnAt(x: number, y: number, yaw: number): void {
    this.robotRoot.visible = true;
    this.robotRoot.position.set(x, y, 0);
    this.robotRoot.rotation.set(0, 0, yaw);

    const forwardX = Math.cos(yaw);
    const forwardY = Math.sin(yaw);
    this.camera.position.set(x - forwardX * CHASE_DISTANCE, y - forwardY * CHASE_DISTANCE, CHASE_HEIGHT);
    this.controls.target.set(x, y, 0.5);
    this.controls.update();

    this.followPrevXY = [x, y];
  }

  /** Move the robot root to a 2D pose (meters, yaw radians about +Z). */
  setPose(x: number, y: number, yaw: number): void {
    this.robotRoot.position.set(x, y, 0);
    this.robotRoot.rotation.set(0, 0, yaw);
    this.robotXY = [x, y];
    this.updateShadowVolume();

    if (this.followCamera) {
      const [prevX, prevY] = this.followPrevXY;
      const dx = x - prevX;
      const dy = y - prevY;
      this.camera.position.x += dx;
      this.camera.position.y += dy;
      this.controls.target.x += dx;
      this.controls.target.y += dy;
      this.followPrevXY = [x, y];
    }
  }

  render(): void {
    const robotCam = this.activeView !== "orbit" ? this.robotCameras.get(this.activeView) : undefined;
    if (!robotCam) this.controls.update();
    this.renderer.render(this.scene, robotCam ?? this.camera);
  }

  /** Release the GL context + control listeners: the SPA router remounts the
   * stage per visit, and undisposed contexts pile up until the browser kills
   * the oldest (~16), breaking the live view. */
  dispose(): void {
    this.controls.dispose();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
  }

  /** Resize the render target (offscreen/stage use). Logical pixels + ratio. */
  setRenderSize(width: number, height: number, pixelRatio = 1): void {
    this.fixedSize = { width, height };
    this.renderer.setPixelRatio(pixelRatio);
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    // Portrait stages (phones): bias the orbit framing so the robot reads in
    // the upper half -- webapp sheets/joystick cover the lower half.
    if (height > width * 1.2) this.camera.setViewOffset(width, height, 0, height * 0.22, width, height);
    else this.camera.clearViewOffset();
    for (const cam of this.robotCameras.values()) {
      cam.aspect = width / height;
      cam.updateProjectionMatrix();
    }
  }

  /** Render the active view into a sub-rectangle of the canvas (logical px,
   * origin bottom-left) -- used for PiP thumbnails. Restores full-canvas
   * viewport afterwards. */
  renderRegion(x: number, y: number, width: number, height: number): void {
    const cam = (this.activeView !== "orbit" ? this.robotCameras.get(this.activeView) : undefined) ?? this.camera;
    const prevAspect = cam.aspect;
    cam.aspect = width / height;
    cam.updateProjectionMatrix();
    this.renderer.setViewport(x, y, width, height);
    this.renderer.setScissor(x, y, width, height);
    this.renderer.setScissorTest(true);
    this.renderer.render(this.scene, cam);
    this.renderer.setScissorTest(false);
    const { width: w, height: h } = this.fixedSize ?? { width: window.innerWidth, height: window.innerHeight };
    this.renderer.setViewport(0, 0, w, h);
    cam.aspect = prevAspect;
    cam.updateProjectionMatrix();
  }

  private viewSize(): { width: number; height: number } {
    return this.fixedSize ?? { width: window.innerWidth, height: window.innerHeight };
  }

  private onResize(): void {
    if (this.fixedSize) return; // stage mode: the ResizeObserver drives sizing
    const { width: w, height: h } = this.viewSize();
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    for (const cam of this.robotCameras.values()) {
      cam.aspect = w / h;
      cam.updateProjectionMatrix();
    }
  }
}

/** Walk up the parent chain to the URDFLink a mesh belongs to, returning its name. */
function nearestLinkName(obj: THREE.Object3D): string | null {
  for (let cur: THREE.Object3D | null = obj; cur; cur = cur.parent) {
    if ((cur as { isURDFLink?: boolean }).isURDFLink) return cur.name;
  }
  return null;
}
