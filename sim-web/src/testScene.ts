// TestScene — minimal Three.js scene for the collision test sandbox (see
// test_world.xml / README "Collision test sandbox"). Just a ground plane
// and two box meshes matching the MuJoCo geometry exactly -- no apartment
// glb, no URDF, no chase camera -- so it starts instantly and stays out of
// the way while watching/pushing the physics.

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

// Must match test_world.xml's cube geoms (box "size" is a half-extent;
// BoxGeometry takes full size).
const CUBE_SIZE = 0.2;

export class TestScene {
  readonly scene = new THREE.Scene();
  readonly camera: THREE.PerspectiveCamera;
  readonly renderer: THREE.WebGLRenderer;
  readonly controls: OrbitControls;

  private cubes = new Map<string, THREE.Mesh>();
  private dragArrow: THREE.ArrowHelper;
  private dragSphere: THREE.Mesh;

  constructor(canvas: HTMLCanvasElement) {
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(window.innerWidth, window.innerHeight);
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFShadowMap;
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;

    this.scene.background = new THREE.Color(0x14161a);

    this.camera = new THREE.PerspectiveCamera(55, window.innerWidth / window.innerHeight, 0.05, 200);
    this.camera.up.set(0, 0, 1);
    this.camera.position.set(1.2, -1.2, 0.9);

    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.target.set(0, 0, 0.2);
    this.controls.minDistance = 0.3;
    this.controls.maxDistance = 20;
    this.controls.update();

    this.addLights();
    this.addGround();

    this.cubes.set("cube_b", this.addCube(0xff7319));
    this.cubes.set("cube_a", this.addCube(0x3d9bff));

    this.dragArrow = new THREE.ArrowHelper(new THREE.Vector3(1, 0, 0), new THREE.Vector3(), 0.4, 0x00ff88, 0.08, 0.05);
    this.dragArrow.visible = false;
    this.scene.add(this.dragArrow);

    this.dragSphere = new THREE.Mesh(
      new THREE.SphereGeometry(0.02, 12, 8),
      new THREE.MeshBasicMaterial({ color: 0x00ff88 }),
    );
    this.dragSphere.visible = false;
    this.scene.add(this.dragSphere);

    window.addEventListener("resize", () => this.onResize());
  }

  private addCube(color: number): THREE.Mesh {
    const mesh = new THREE.Mesh(
      new THREE.BoxGeometry(CUBE_SIZE, CUBE_SIZE, CUBE_SIZE),
      new THREE.MeshStandardMaterial({ color, metalness: 0.2, roughness: 0.6 }),
    );
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    this.scene.add(mesh);
    return mesh;
  }

  private addLights(): void {
    this.scene.add(new THREE.AmbientLight(0xffffff, 1.2));

    const key = new THREE.DirectionalLight(0xffffff, 2.0);
    key.position.set(2, -1.5, 3);
    key.castShadow = true;
    key.shadow.mapSize.set(2048, 2048);
    key.shadow.camera.near = 0.1;
    key.shadow.camera.far = 15;
    key.shadow.camera.left = -3;
    key.shadow.camera.right = 3;
    key.shadow.camera.top = 3;
    key.shadow.camera.bottom = -3;
    key.shadow.bias = -0.0004;
    this.scene.add(key);

    this.scene.add(new THREE.HemisphereLight(0xaabbcc, 0x445566, 1.2));
  }

  private addGround(): void {
    // Matches test_world.xml's ground plane (10x10m, centered at origin).
    const ground = new THREE.Mesh(
      new THREE.PlaneGeometry(10, 10),
      new THREE.MeshStandardMaterial({ color: 0x2a2d33, roughness: 0.9 }),
    );
    ground.receiveShadow = true;
    this.scene.add(ground);

    const grid = new THREE.GridHelper(10, 20, 0x3a3d43, 0x24262b);
    grid.rotation.x = Math.PI / 2;
    grid.position.z = 0.001; // avoid z-fighting with the ground plane
    this.scene.add(grid);
  }

  /** Tag a cube mesh with its MuJoCo body id, for drag-force raycasting. */
  setBodyId(name: string, bodyId: number): void {
    const mesh = this.cubes.get(name);
    if (mesh) mesh.userData.bodyId = bodyId;
  }

  /** Move a cube to match physics (full 3D pose -- cubes can tumble). */
  setBodyPose(name: string, x: number, y: number, z: number, qw: number, qx: number, qy: number, qz: number): void {
    const mesh = this.cubes.get(name);
    if (!mesh) return;
    mesh.position.set(x, y, z);
    mesh.quaternion.set(qx, qy, qz, qw);
  }

  /** Raycast the cubes from normalized device coords; returns the hit cube + world point, or null. */
  pickCube(ndcX: number, ndcY: number): { name: string; bodyId: number; point: THREE.Vector3 } | null {
    const raycaster = new THREE.Raycaster();
    raycaster.setFromCamera(new THREE.Vector2(ndcX, ndcY), this.camera);
    const entries = [...this.cubes.entries()];
    const hits = raycaster.intersectObjects(
      entries.map(([, mesh]) => mesh),
      false,
    );
    if (!hits.length) return null;
    const hit = hits[0];
    const name = entries.find(([, mesh]) => mesh === hit.object)?.[0];
    const bodyId = (hit.object as THREE.Mesh).userData.bodyId as number | undefined;
    if (!name || bodyId === undefined) return null;
    return { name, bodyId, point: hit.point.clone() };
  }

  /** Current world position of a tracked cube, or null if unknown. */
  getBodyPosition(name: string): THREE.Vector3 | null {
    return this.cubes.get(name)?.position.clone() ?? null;
  }

  /** Cast a ray from normalized device coords and intersect a plane; returns the world point, or null. */
  raycastPlane(ndcX: number, ndcY: number, plane: THREE.Plane): THREE.Vector3 | null {
    const raycaster = new THREE.Raycaster();
    raycaster.setFromCamera(new THREE.Vector2(ndcX, ndcY), this.camera);
    const out = new THREE.Vector3();
    return raycaster.ray.intersectPlane(plane, out) ? out : null;
  }

  showDragVisual(anchor: THREE.Vector3, current: THREE.Vector3): void {
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
    this.dragArrow.visible = false;
    this.dragSphere.visible = false;
  }

  render(): void {
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }

  private onResize(): void {
    const w = window.innerWidth;
    const h = window.innerHeight;
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }
}
