# Arm SDK playground

Backend for the webapp's **Arm SDK** page (`/armsdk`): a localhost JSON API
that drives the real `brain_client` Manipulation SDK (imported from this
checkout's source tree) against the live arm.

```bash
scripts/arm-sdk-playground/run.sh
```

Then open the webapp and pick **Arm SDK** in the rail. The page gives you a
live 3D URDF mirror of the robot (drag the amber handle to move the EE),
cartesian jogging with target-vs-settled error readouts, live joint sliders
(streamed via `stream_joints`), gripper control, and copy buttons for joint
positions and poses.

The front door proxies `/armsdk/api/*` to this server (port 8090, localhost
only) and serves the URDF + meshes at `/armsdk/model/*`, so nothing here is
reachable off-box except through the webapp.
