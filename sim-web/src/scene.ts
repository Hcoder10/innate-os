// SimScene — Three.js scene: the apartment.glb environment (visual only —
// physics collision against it happens separately in apartmentWorker.ts)
// plus the real MARS robot loaded from its ROS URDF.
//
// Scene convention: Z-up, X-forward, matching ROS's REP-103 (and /odom) so
// there's no axis juggling once this is wired to rosbridge in Mode B.
// urdf-loader instantiates the URDF without any frame remap (its meshes are
// already Z-up as authored), so the robot needs no rotation; the apartment
// glb is authored Y-up (the glTF convention) and gets rotated into the
// scene's Z-up frame on load.

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OBJLoader } from "three/addons/loaders/OBJLoader.js";
import URDFLoader from "urdf-loader";
import type { URDFRobot } from "urdf-loader";

const APARTMENT_URL = "/models/appartement.glb";
const ROBOT_URDF_URL = "/robot/mars.urdf";

// The two collision sources apartmentWorker.ts can load into the physics
// model (manifest.json lists every OBJ filename since a browser can't list
// a directory): "hulls" is the per-room CoACD hull set (see
// sim-mujoco/README.md's "Collision mesh regeneration"), "sdf" the
// watertight voxel-shell meshes its native-SDF mode collides against
// (sim-mujoco/build_sdf_shells.py). Same +90deg-about-X rotation as the
// apartment.glb load below (Y-up source -> Z-up scene).
const COLLISION_DEBUG_URLS = {
  hulls: "/physics/apartment_collisions_v2/",
  sdf: "/physics/apartment_sdf/",
} as const;
export type CollisionDebugSource = keyof typeof COLLISION_DEBUG_URLS;

// These URDF links carry no real body geometry — just small marker spheres
// used to visualize the end-effector / camera optical frames (e.g. in
// RViz). Hide them here rather than in the URDF itself, since that file is
// shared with the rest of the ROS stack.
const HIDDEN_FRAME_LINKS: string[] = ["ee_link", "head_camera_left", "head_camera_right"];

// Arm links painted orange, matching Genesis's SIM_ROBOT_ORANGE_LINKS. Every
// URDF link actually shares the matt_black material, so (like Genesis) we
// override by link name rather than by material.
const ORANGE_LINKS = new Set(["link1", "link3", "link5"]);

// Chase-cam framing used when a pose is snapped in (see spawnAt below).
const CHASE_DISTANCE = 1.8; // meters behind the robot
const CHASE_HEIGHT = 1.1; // meters above the ground

// Robot-mounted camera views: same frames, axis conventions, FOV and near
// plane as the genesis sim (simulation_node.py's _init_camera_link_refs).
export type CameraView = "orbit" | "main" | "arm";
const ROBOT_CAMERA_VFOV = 80;
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

  private robotRoot = new THREE.Group();
  private robot?: URDFRobot;
  private followPrevXY: [number, number] = [0, 0];
  private glossyMaterialCache = new Map<THREE.Material, THREE.MeshStandardMaterial>();
  private orange?: THREE.MeshStandardMaterial;
  private dragArrow?: THREE.ArrowHelper;
  private dragSphere?: THREE.Mesh;
  private collisionDebugGroups = new Map<CollisionDebugSource, THREE.Group>();
  private collisionDebugSource: CollisionDebugSource = "hulls";
  private collisionDebugVisible = false;
  private robotCameras = new Map<CameraView, THREE.PerspectiveCamera>();
  private activeView: CameraView = "orbit";
  private lidarPoints?: THREE.Points;

  constructor(canvas: HTMLCanvasElement) {
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(window.innerWidth, window.innerHeight);
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFShadowMap;
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.5;

    this.scene.background = new THREE.Color(0x14161a);
    this.scene.fog = new THREE.FogExp2(0x14161a, 0.035);

    this.camera = new THREE.PerspectiveCamera(55, window.innerWidth / window.innerHeight, 0.05, 200);
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
    this.scene.add(this.robotRoot);

    window.addEventListener("resize", () => this.onResize());
  }

  private addLights(): void {
    // No environment map on purpose — a PMREM env map averages a uniform
    // reflection over the whole surface, which grey-washes dark, low-
    // roughness materials. Multiple directional lights from different
    // angles give distinct specular highlights instead, which is what
    // actually reads as "glossy" for the matte-black/metalness-boosted
    // robot parts (see loadRobot below).
    this.scene.add(new THREE.AmbientLight(0xffffff, 1.2));

    const key = new THREE.DirectionalLight(0xffffff, 2.0);
    key.position.set(4, -3, 6);
    key.castShadow = true;
    key.shadow.mapSize.set(2048, 2048);
    key.shadow.camera.near = 0.1;
    key.shadow.camera.far = 30;
    key.shadow.camera.left = -8;
    key.shadow.camera.right = 8;
    key.shadow.camera.top = 8;
    key.shadow.camera.bottom = -8;
    // The robot self-shadows against a coarse shadow map (a 16m frustum over
    // ~2cm detail), producing diagonal shadow-acne stripes on its own
    // surfaces. normalBias offsets the shadow lookup along the surface normal,
    // which clears acne on detailed meshes without the peter-panning that a
    // large depth bias causes.
    key.shadow.bias = -0.0004;
    key.shadow.normalBias = 0.05;
    this.scene.add(key);

    const fill = new THREE.DirectionalLight(0xaaccff, 0.8);
    fill.position.set(-4, 3, 3);
    this.scene.add(fill);

    this.scene.add(new THREE.HemisphereLight(0xaabbcc, 0x445566, 1.2));
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

  async loadApartment(): Promise<void> {
    const loader = new GLTFLoader();
    const gltf = await loader.loadAsync(APARTMENT_URL);
    const root = gltf.scene;
    root.rotation.x = Math.PI / 2; // glTF Y-up -> scene Z-up
    root.traverse((obj) => {
      if (obj instanceof THREE.Mesh) {
        obj.receiveShadow = true;
        // appartement.glb's materials are exported with doubleSided:true,
        // so GLTFLoader assigns THREE.DoubleSide and every wall/ceiling
        // renders from both sides. Forcing FrontSide instead makes MuJoCo's
        // renderer-default single-sided culling behavior match here too:
        // a wall/ceiling triangle only draws when viewed from the side its
        // normal points to (into the room, as authored), so a camera
        // outside/above the apartment sees straight through to the
        // interior instead of a solid shell -- the "dollhouse cutaway"
        // look, useful for orbit/overview cameras. If some geometry has
        // inconsistent winding and ends up invisible from the inside too,
        // flip this to THREE.BackSide instead.
        const setFrontSide = (mat: THREE.Material) => {
          mat.side = THREE.FrontSide;
        };
        if (Array.isArray(obj.material)) obj.material.forEach(setFrontSide);
        else setFrontSide(obj.material);
      }
    });
    this.scene.add(root);
  }

  /**
   * Loads a collision source (same files apartmentWorker.ts compiles into
   * the live model, see its loadApartmentCollisions) as a bright wireframe
   * overlay, rotated the same way that worker rotates the apartment body, so
   * it's directly comparable to the apartment.glb visual for checking
   * alignment. Call setCollisionDebugVisible to toggle; loaded sources are
   * cached, so switching back and forth only fetches once.
   */
  async loadCollisionDebug(source: CollisionDebugSource = "hulls"): Promise<void> {
    if (this.collisionDebugGroups.has(source)) return;

    const group = new THREE.Group();
    group.rotation.x = Math.PI / 2; // same +90deg X as apartmentWorker.ts's apartment body quat
    group.visible = false;

    const baseUrl = COLLISION_DEBUG_URLS[source];
    const manifest: string[] = await (await fetch(`${baseUrl}manifest.json`)).json();
    const loader = new OBJLoader();
    const material = new THREE.MeshBasicMaterial({ color: 0x00ff88, wireframe: true });
    const loads = manifest.map(async (filename) => {
      const obj = await loader.loadAsync(`${baseUrl}${filename}`);
      obj.traverse((child) => {
        if (child instanceof THREE.Mesh) child.material = material;
      });
      group.add(obj);
    });
    await Promise.all(loads);

    this.scene.add(group);
    this.collisionDebugGroups.set(source, group);
    this.updateCollisionDebugVisibility();
  }

  /** Which collision source the overlay shows -- keep in sync with the mode
   * the physics worker was init'd with. Loads the source if needed. */
  async setCollisionDebugSource(source: CollisionDebugSource): Promise<void> {
    this.collisionDebugSource = source;
    await this.loadCollisionDebug(source);
    this.updateCollisionDebugVisibility();
  }

  setCollisionDebugVisible(visible: boolean): void {
    this.collisionDebugVisible = visible;
    this.updateCollisionDebugVisibility();
  }

  /** Update the lidar overlay (connected mode) with world-frame hit points. */
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

  private updateCollisionDebugVisibility(): void {
    for (const [source, group] of this.collisionDebugGroups) {
      group.visible = this.collisionDebugVisible && source === this.collisionDebugSource;
    }
  }

  async loadRobot(): Promise<URDFRobot> {
    // URDFLoader.loadAsync resolves once the URDF XML is parsed, but the STL
    // meshes are loaded asynchronously through this LoadingManager and only
    // attached to the tree afterward. Wait on the manager's onLoad so every
    // mesh exists before we restyle — otherwise traverse only catches the
    // synchronous URDF primitives (the marker spheres) and the STL bodies
    // keep their default grey material.
    const manager = new THREE.LoadingManager();
    const allMeshesLoaded = new Promise<void>((resolve) => {
      manager.onLoad = () => resolve();
    });

    const loader = new URDFLoader(manager);
    loader.packages = { mars_sim: "/robot" };
    const robot = await loader.loadAsync(ROBOT_URDF_URL);
    await allMeshesLoaded;

    for (const name of HIDDEN_FRAME_LINKS) {
      const link = robot.links[name];
      if (link) link.visible = false;
    }

    robot.traverse((obj) => {
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
        window.innerWidth / window.innerHeight,
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

  // The URDF's plain MeshPhongMaterials (untextured matte plastic) look flat
  // under direct lighting. Swap to a PBR MeshStandardMaterial. Moderate
  // metalness keeps a soft sheen while leaving enough diffuse response that
  // the body is lit from all sides (pure metal + no env map reads as a
  // near-black void), and the higher roughness keeps it matte rather than
  // mirror-like.
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

  /** Current world position of the robot root (always z=0, see spawnAt/setPose). */
  getRobotPosition(): THREE.Vector3 {
    return this.robotRoot.position.clone();
  }

  /** Raycast the robot's meshes from normalized device coords; returns the world hit point, or null. */
  raycastRobot(ndcX: number, ndcY: number): THREE.Vector3 | null {
    const raycaster = new THREE.Raycaster();
    raycaster.setFromCamera(new THREE.Vector2(ndcX, ndcY), this.camera);
    const hits = raycaster.intersectObject(this.robotRoot, true);
    return hits.length ? hits[0].point.clone() : null;
  }

  /** Cast a ray from normalized device coords and intersect a plane; returns the world point, or null. */
  raycastPlane(ndcX: number, ndcY: number, plane: THREE.Plane): THREE.Vector3 | null {
    const raycaster = new THREE.Raycaster();
    raycaster.setFromCamera(new THREE.Vector2(ndcX, ndcY), this.camera);
    const out = new THREE.Vector3();
    return raycaster.ray.intersectPlane(plane, out) ? out : null;
  }

  /** Small arrow + sphere marking an active drag force (see e.g. drive-test-main.ts). */
  showDragVisual(anchor: THREE.Vector3, current: THREE.Vector3): void {
    if (!this.dragArrow) {
      this.dragArrow = new THREE.ArrowHelper(new THREE.Vector3(1, 0, 0), new THREE.Vector3(), 0.4, 0x00ff88, 0.08, 0.05);
      this.scene.add(this.dragArrow);
    }
    if (!this.dragSphere) {
      this.dragSphere = new THREE.Mesh(
        new THREE.SphereGeometry(0.02, 12, 8),
        new THREE.MeshBasicMaterial({ color: 0x00ff88 }),
      );
      this.scene.add(this.dragSphere);
    }
    const diff = current.clone().sub(anchor);
    const len = diff.length();
    this.dragSphere.position.copy(anchor);
    this.dragSphere.visible = true;
    if (len < 1e-4) {
      this.dragArrow.visible = false;
      return;
    }
    this.dragArrow.visible = true;
    this.dragArrow.position.copy(anchor);
    this.dragArrow.setDirection(diff.normalize());
    this.dragArrow.setLength(Math.min(len * 3, 1.0), 0.08, 0.05);
  }

  hideDragVisual(): void {
    if (this.dragArrow) this.dragArrow.visible = false;
    if (this.dragSphere) this.dragSphere.visible = false;
  }

  render(): void {
    const robotCam = this.activeView !== "orbit" ? this.robotCameras.get(this.activeView) : undefined;
    if (!robotCam) this.controls.update();
    this.renderer.render(this.scene, robotCam ?? this.camera);
  }

  private onResize(): void {
    const w = window.innerWidth;
    const h = window.innerHeight;
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
