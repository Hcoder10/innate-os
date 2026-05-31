#!/usr/bin/env python3
"""Genesis smoke test for scripted Maurice cube pickup.

This intentionally stays smaller than the full FastAPI/ROS simulator stack: it
uses the same Genesis dependency and the current ROS Maurice URDF from this
repo, adds a rigid cube, runs a preprogrammed grasp/lift motion, and records an
MP4 plus numeric lift metrics.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import cv2
import imageio.v2 as imageio
import numpy as np
import torch


SIM_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SIM_ROOT.parent
DEFAULT_OUT_DIR = SIM_ROOT / "artifacts" / "pickup_cube"
ROBOT_URDF = (
    REPO_ROOT
    / "ros2_ws"
    / "src"
    / "maurice_bot"
    / "maurice_sim"
    / "urdf"
    / "maurice.urdf"
)


@dataclass
class TrialMetrics:
    output_video: str
    output_contact_sheet: str
    output_metrics: str
    output_trace: str
    cube_size_m: float
    object_size_m: tuple[float, float, float]
    cube_initial_z_m: float
    cube_final_z_m: float
    cube_max_z_m: float
    cube_lift_delta_m: float
    cube_final_lift_delta_m: float
    lift_threshold_m: float
    held_threshold_m: float
    pickup_success: bool
    held_after_lift: bool
    contact_valid_pickup: bool
    gripper_cube_contact_steps: int
    max_gripper_cube_contacts: int
    max_gripper_cube_penetration_m: float
    max_gripper_cube_contact_force_n: float
    max_allowed_penetration_m: float
    max_tcp_error_m: float
    backend: str
    genesis_version: str
    robot_friction: float
    cube_friction: float
    open_angle_rad: float
    close_angle_rad: float
    notes: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run and record a scripted Genesis cube pickup smoke test."
    )
    parser.add_argument("--backend", default="cpu", choices=["cpu", "metal", "gpu"])
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--name", default=None)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--render-every", type=int, default=3)
    parser.add_argument("--dt", type=float, default=0.01)
    parser.add_argument("--substeps", type=int, default=4)
    parser.add_argument("--cube-size", type=float, default=0.030)
    parser.add_argument(
        "--object-size",
        type=float,
        nargs=3,
        metavar=("X", "Y", "Z"),
        default=None,
        help="Override cube size with a box object size in meters.",
    )
    parser.add_argument("--cube-x", type=float, default=0.340)
    parser.add_argument("--cube-y", type=float, default=-0.05285)
    parser.add_argument("--grasp-z-offset", type=float, default=0.015)
    parser.add_argument("--lift-height", type=float, default=0.120)
    parser.add_argument("--lift-threshold", type=float, default=0.040)
    parser.add_argument("--held-threshold", type=float, default=0.025)
    parser.add_argument("--max-allowed-penetration", type=float, default=0.004)
    # Current ROS URDF on the recent simulator branch uses joint6=0.3491 as open
    # and joint6=0.0 as the practical closed pinch.
    parser.add_argument("--open-angle", type=float, default=0.3491)
    parser.add_argument("--close-angle", type=float, default=0.0)
    parser.add_argument("--cube-friction", type=float, default=5.0)
    parser.add_argument("--robot-friction", type=float, default=5.0)
    parser.add_argument("--noslip-iterations", type=int, default=5)
    parser.add_argument("--solver-iterations", type=int, default=80)
    parser.add_argument("--contact-pruning-tolerance", type=float, default=0.001)
    parser.add_argument("--constraint-timeconst", type=float, default=0.002)
    parser.add_argument("--camera-pos", type=float, nargs=3, default=(0.56, -0.62, 0.35))
    parser.add_argument("--camera-lookat", type=float, nargs=3, default=None)
    parser.add_argument("--camera-fov", type=float, default=35)
    parser.add_argument("--show-viewer", action="store_true")
    parser.add_argument(
        "--drive-mode",
        default="position",
        choices=["position", "teleport"],
        help=(
            "Use Genesis joint position controllers or direct set_dofs_position. "
            "The app simulator currently uses direct setters; position mode is "
            "better for testing whether contacts can sustain grasp force."
        ),
    )
    parser.add_argument(
        "--use-light-rigid-options",
        action="store_true",
        help=(
            "Use options close to the app simulator default. The default for this "
            "script is the heavier manipulation-oriented contact setup."
        ),
    )
    parser.add_argument(
        "--ghost-cube-on-lift",
        action="store_true",
        help=(
            "Debug fallback only: attach the cube to the tool during lift. This "
            "bypasses physical contact and should not be counted as a pickup."
        ),
    )
    return parser.parse_args()


def wxyz_to_xyzw(quat: np.ndarray) -> np.ndarray:
    return np.array([quat[1], quat[2], quat[3], quat[0]], dtype=float)


def transform_local_point(link, local_point: Iterable[float]) -> np.ndarray:
    from scipy.spatial.transform import Rotation as R

    pos = link.get_pos().cpu().numpy()
    quat_wxyz = link.get_quat().cpu().numpy()
    rot = R.from_quat(wxyz_to_xyzw(quat_wxyz))
    return pos + rot.apply(np.array(local_point, dtype=float))


def lerp(a: np.ndarray, b: np.ndarray, alpha: float) -> np.ndarray:
    return a + (b - a) * alpha


def make_video_writer(path: Path, fps: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    return imageio.get_writer(
        str(path),
        fps=fps,
        codec="libx264",
        quality=8,
        macro_block_size=16,
    )


def write_contact_sheet(frames: list[np.ndarray], path: Path) -> None:
    if not frames:
        return

    sample_count = min(12, len(frames))
    indices = np.linspace(0, len(frames) - 1, sample_count, dtype=int)
    thumbs = []
    for idx in indices:
        frame = frames[idx]
        thumb = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
        cv2.putText(
            thumb,
            f"f{idx:03d}",
            (10, 26),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        thumbs.append(thumb)

    while len(thumbs) % 4:
        thumbs.append(np.zeros_like(thumbs[0]))

    rows = []
    for start in range(0, len(thumbs), 4):
        rows.append(np.concatenate(thumbs[start : start + 4], axis=1))

    sheet = np.concatenate(rows, axis=0)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), cv2.cvtColor(sheet, cv2.COLOR_RGB2BGR))


def set_gripper(qpos: np.ndarray, robot, angle: float) -> np.ndarray:
    q = qpos.copy()
    joint6 = robot.get_joint("joint6").dofs_idx_local[0]
    joint6m = robot.get_joint("joint6M").dofs_idx_local[0]
    q[joint6] = angle
    q[joint6m] = -angle
    return q


def set_robot_qpos(robot, qpos: np.ndarray) -> None:
    robot.set_dofs_position(
        torch.tensor(qpos, dtype=torch.float32),
        zero_velocity=True,
    )


def render_frame(camera) -> np.ndarray:
    frame, _, _, _ = camera.render()
    return frame


def main() -> int:
    args = parse_args()

    import genesis as gs

    run_name = args.name or datetime.now().strftime("pickup_cube_%Y%m%d_%H%M%S")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    video_path = args.out_dir / f"{run_name}.mp4"
    sheet_path = args.out_dir / f"{run_name}_contact_sheet.jpg"
    metrics_path = args.out_dir / f"{run_name}_metrics.json"
    trace_path = args.out_dir / f"{run_name}_trace.csv"

    gs.init(backend=getattr(gs, args.backend), logging_level="warning")

    if args.use_light_rigid_options:
        rigid_options = gs.options.RigidOptions(
            enable_self_collision=False,
            max_collision_pairs=64,
            iterations=20,
            ls_iterations=10,
        )
    else:
        rigid_options = gs.options.RigidOptions(
            enable_collision=True,
            enable_self_collision=True,
            enable_multi_contact=True,
            box_box_detection=True,
            noslip_iterations=args.noslip_iterations,
            iterations=args.solver_iterations,
            ls_iterations=50,
            constraint_timeconst=args.constraint_timeconst,
            contact_pruning_tolerance=args.contact_pruning_tolerance,
            max_collision_pairs=256,
        )

    camera_lookat = (
        tuple(float(v) for v in args.camera_lookat)
        if args.camera_lookat is not None
        else (args.cube_x, args.cube_y, 0.08)
    )
    camera_pos = tuple(float(v) for v in args.camera_pos)

    scene = gs.Scene(
        sim_options=gs.options.SimOptions(dt=args.dt, substeps=args.substeps),
        rigid_options=rigid_options,
        viewer_options=gs.options.ViewerOptions(
            camera_pos=camera_pos,
            camera_lookat=camera_lookat,
            camera_fov=args.camera_fov,
            res=(1280, 720),
        ),
        vis_options=gs.options.VisOptions(ambient_light=(0.55, 0.55, 0.55)),
        show_viewer=args.show_viewer,
        show_FPS=False,
    )

    scene.add_entity(
        gs.morphs.Plane(pos=(0, 0, 0)),
        material=gs.materials.Rigid(friction=2.0),
        surface=gs.surfaces.Rough(color=(0.45, 0.48, 0.50, 1.0)),
    )

    robot = scene.add_entity(
        gs.morphs.URDF(
            file=str(ROBOT_URDF),
            pos=(0, 0, 0.05),
            quat=(1, 0, 0, 0),
            fixed=True,
        ),
        material=gs.materials.Rigid(friction=args.robot_friction),
    )

    object_size = (
        tuple(float(v) for v in args.object_size)
        if args.object_size is not None
        else (args.cube_size, args.cube_size, args.cube_size)
    )
    cube_center_z = object_size[2] / 2
    cube = scene.add_entity(
        gs.morphs.Box(
            size=object_size,
            pos=(args.cube_x, args.cube_y, cube_center_z),
            fixed=False,
        ),
        material=gs.materials.Rigid(
            rho=120.0,
            friction=args.cube_friction,
            coup_friction=args.cube_friction,
        ),
        surface=gs.surfaces.Rough(color=(0.95, 0.58, 0.12, 1.0)),
        visualize_contact=True,
        name="pickup_cube",
    )

    camera = scene.add_camera(
        res=(1280, 720),
        pos=camera_pos,
        lookat=camera_lookat,
        fov=args.camera_fov,
    )

    scene.build()

    link5 = robot.get_link("link5")
    arm_dofs = [
        robot.get_joint(joint).dofs_idx_local[0]
        for joint in ["joint1", "joint2", "joint3", "joint4", "joint5"]
    ]
    tcp_local = np.array([0.064, 0.0, 0.0])
    arm_and_gripper_dofs = arm_dofs + [
        robot.get_joint("joint6").dofs_idx_local[0],
        robot.get_joint("joint6M").dofs_idx_local[0],
    ]

    if args.drive_mode == "position":
        robot.set_dofs_kp(
            np.array([500, 500, 500, 500, 300, 150, 150], dtype=float),
            dofs_idx_local=arm_and_gripper_dofs,
        )
        robot.set_dofs_kv(
            np.array([35, 35, 35, 35, 25, 8, 8], dtype=float),
            dofs_idx_local=arm_and_gripper_dofs,
        )
        robot.set_dofs_force_range(
            np.array([-25, -25, -25, -25, -15, -12, -12], dtype=float),
            np.array([25, 25, 25, 25, 15, 12, 12], dtype=float),
            dofs_idx_local=arm_and_gripper_dofs,
        )

    def solve_tcp(target_pos: np.ndarray, init_qpos: np.ndarray | None = None) -> np.ndarray:
        qpos = robot.inverse_kinematics_multilink(
            links=[link5],
            poss=[target_pos],
            local_points=[tcp_local],
            init_qpos=init_qpos,
            respect_joint_limit=True,
            max_samples=25,
            max_solver_iters=80,
            damping=0.01,
            pos_tol=0.001,
            dofs_idx_local=arm_dofs,
        )
        return qpos.cpu().numpy()

    grasp_target = np.array(
        [args.cube_x, args.cube_y, cube_center_z + args.grasp_z_offset],
        dtype=float,
    )
    pregrasp_target = grasp_target + np.array([0.0, 0.0, 0.10])
    lift_target = grasp_target + np.array([0.0, 0.0, args.lift_height])

    q_pre = set_gripper(solve_tcp(pregrasp_target), robot, args.open_angle)
    q_grasp = set_gripper(solve_tcp(grasp_target, q_pre), robot, args.open_angle)

    writer = make_video_writer(video_path, args.fps)
    saved_frames: list[np.ndarray] = []
    trace_rows = []
    max_tcp_error = 0.0
    gripper_cube_contact_steps = 0
    max_gripper_cube_contacts = 0
    max_gripper_cube_penetration = 0.0
    max_gripper_cube_contact_force = 0.0
    step_index = 0
    current_target = [pregrasp_target]
    gripper_geom_idxs = {
        geom.idx
        for link_name in ("link61", "link62")
        for geom in robot.get_link(link_name).geoms
    }

    def apply_qpos(qpos: np.ndarray) -> None:
        if args.drive_mode == "position":
            robot.control_dofs_position(
                qpos[arm_and_gripper_dofs],
                dofs_idx_local=arm_and_gripper_dofs,
            )
        else:
            set_robot_qpos(robot, qpos)

    def step_and_maybe_record(label: str) -> None:
        nonlocal step_index, max_tcp_error
        nonlocal gripper_cube_contact_steps, max_gripper_cube_contacts
        nonlocal max_gripper_cube_penetration, max_gripper_cube_contact_force
        if args.ghost_cube_on_lift and label.startswith("lift"):
            cube.set_pos(transform_local_point(link5, tcp_local))

        scene.step()
        cube_pos = cube.get_pos().cpu().numpy()
        tcp_pos = transform_local_point(link5, tcp_local)
        tcp_error = float(np.linalg.norm(tcp_pos - current_target[0]))
        max_tcp_error = max(max_tcp_error, tcp_error)
        contacts = cube.get_contacts(with_entity=robot)
        contact_count = 0
        contact_penetration = 0.0
        contact_force = 0.0
        if contacts["geom_a"].numel():
            geom_a = contacts["geom_a"].cpu().numpy()
            geom_b = contacts["geom_b"].cpu().numpy()
            mask = np.array(
                [
                    int(a) in gripper_geom_idxs or int(b) in gripper_geom_idxs
                    for a, b in zip(geom_a, geom_b)
                ],
                dtype=bool,
            )
            if mask.any():
                penetrations = contacts["penetration"].cpu().numpy()[mask]
                forces = contacts["force_a"].cpu().numpy()[mask]
                contact_count = int(mask.sum())
                contact_penetration = float(penetrations.max())
                contact_force = float(np.linalg.norm(forces, axis=1).max())
                gripper_cube_contact_steps += 1
                max_gripper_cube_contacts = max(max_gripper_cube_contacts, contact_count)
                max_gripper_cube_penetration = max(
                    max_gripper_cube_penetration, contact_penetration
                )
                max_gripper_cube_contact_force = max(
                    max_gripper_cube_contact_force, contact_force
                )

        trace_rows.append(
            {
                "step": step_index,
                "label": label,
                "cube_x": float(cube_pos[0]),
                "cube_y": float(cube_pos[1]),
                "cube_z": float(cube_pos[2]),
                "tcp_x": float(tcp_pos[0]),
                "tcp_y": float(tcp_pos[1]),
                "tcp_z": float(tcp_pos[2]),
                "tcp_error": tcp_error,
                "gripper_cube_contacts": contact_count,
                "gripper_cube_penetration": contact_penetration,
                "gripper_cube_contact_force": contact_force,
            }
        )

        if step_index % args.render_every == 0:
            frame = render_frame(camera)
            writer.append_data(frame)
            if len(saved_frames) < 180:
                saved_frames.append(frame)
        step_index += 1

    set_robot_qpos(robot, q_pre)
    for _ in range(90):
        apply_qpos(q_pre)
        step_and_maybe_record("settle_open_above_cube")

    prev_q = q_pre.copy()
    for i in range(60):
        alpha = (i + 1) / 60
        current_target[0] = lerp(pregrasp_target, grasp_target, alpha)
        q = set_gripper(solve_tcp(current_target[0], prev_q), robot, args.open_angle)
        apply_qpos(q)
        prev_q = q
        step_and_maybe_record("descend_open")

    for i in range(80):
        alpha = (i + 1) / 80
        angle = float(lerp(np.array(args.open_angle), np.array(args.close_angle), alpha))
        q = set_gripper(prev_q, robot, angle)
        apply_qpos(q)
        prev_q = q
        current_target[0] = grasp_target
        step_and_maybe_record("close_gripper")

    for _ in range(70):
        apply_qpos(prev_q)
        step_and_maybe_record("hold_closed_before_lift")

    for i in range(90):
        alpha = (i + 1) / 90
        current_target[0] = lerp(grasp_target, lift_target, alpha)
        q = set_gripper(solve_tcp(current_target[0], prev_q), robot, args.close_angle)
        apply_qpos(q)
        prev_q = q
        step_and_maybe_record("lift_closed")

    current_target[0] = lift_target
    for _ in range(80):
        apply_qpos(prev_q)
        step_and_maybe_record("hold_closed_after_lift")

    writer.close()

    with trace_path.open("w", newline="") as f:
        writer_csv = csv.DictWriter(f, fieldnames=list(trace_rows[0].keys()))
        writer_csv.writeheader()
        writer_csv.writerows(trace_rows)

    write_contact_sheet(saved_frames, sheet_path)

    cube_zs = [row["cube_z"] for row in trace_rows]
    cube_initial_z = float(np.median(cube_zs[:30]))
    cube_final_z = float(np.median(cube_zs[-30:]))
    cube_max_z = float(max(cube_zs))
    lift_delta = cube_max_z - cube_initial_z
    final_lift_delta = cube_final_z - cube_initial_z
    pickup_success = lift_delta >= args.lift_threshold
    held_after_lift = final_lift_delta >= args.held_threshold
    contact_valid_pickup = (
        pickup_success
        and held_after_lift
        and gripper_cube_contact_steps > 0
        and max_gripper_cube_penetration <= args.max_allowed_penetration
    )

    metrics = TrialMetrics(
        output_video=str(video_path),
        output_contact_sheet=str(sheet_path),
        output_metrics=str(metrics_path),
        output_trace=str(trace_path),
        cube_size_m=args.cube_size,
        object_size_m=object_size,
        cube_initial_z_m=cube_initial_z,
        cube_final_z_m=cube_final_z,
        cube_max_z_m=cube_max_z,
        cube_lift_delta_m=lift_delta,
        cube_final_lift_delta_m=final_lift_delta,
        lift_threshold_m=args.lift_threshold,
        held_threshold_m=args.held_threshold,
        pickup_success=pickup_success,
        held_after_lift=held_after_lift,
        contact_valid_pickup=contact_valid_pickup,
        gripper_cube_contact_steps=gripper_cube_contact_steps,
        max_gripper_cube_contacts=max_gripper_cube_contacts,
        max_gripper_cube_penetration_m=max_gripper_cube_penetration,
        max_gripper_cube_contact_force_n=max_gripper_cube_contact_force,
        max_allowed_penetration_m=args.max_allowed_penetration,
        max_tcp_error_m=max_tcp_error,
        backend=args.backend,
        genesis_version=getattr(gs, "__version__", "unknown"),
        robot_friction=args.robot_friction,
        cube_friction=args.cube_friction,
        open_angle_rad=args.open_angle,
        close_angle_rad=args.close_angle,
        notes=(
            "Uses this branch's ROS Maurice URDF and Genesis rigid bodies. "
            f"drive_mode={args.drive_mode}. This validates scripted grasp "
            "contact response, not a learned or e2e policy."
        ),
    )

    metrics_path.write_text(json.dumps(asdict(metrics), indent=2) + "\n")
    print(json.dumps(asdict(metrics), indent=2))
    return 0 if contact_valid_pickup else 2


if __name__ == "__main__":
    raise SystemExit(main())
