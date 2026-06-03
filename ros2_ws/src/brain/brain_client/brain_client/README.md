# `brain_client` — package layout

The package is organised **by robot concept**, not by architecture layer. To find
code, start from what it *does*:

| Folder | What lives here |
|---|---|
| `nodes/` | The runnable ROS entry points (the only files with `main()`). Thin composition roots — they build collaborators, wire them, and spin. No behaviour. |
| `core/` | The brain's own behaviour: the perception loop (`orchestrator`), the activate/deactivate state machine (`lifecycle`), reacting to the agent's next task (`vision_output`), typed `config`, and the shared `state`. |
| `perception/` | Turning sensors into what the agent sees: `camera`, `pose`/`pose_tracking`, `image_codec`, `gaze`. |
| `navigation/` | Map state and the navigation `payload` sent to the cloud agent. |
| `skills/` | The skill system: `registry`, `runner` (action lifecycle), `registration`, `loader`, `hot_reload`, and the public `types` SDK base classes. |
| `agents/` | Directives/behaviours: `loader`, `initializer`, and the public `types` SDK base class. |
| `inputs/` | Input-device subsystem and its public `types` SDK base class. |
| `comms/` | Talking to the cloud + user: the in-process WebSocket transport (`ws_manager` + `ws_transport` + pure `ws_config`), `websocket` (the bridge with the threadsafe asyncio→executor hand-off), `messages` (typed contracts), `chat`, `tts`. |
| `robot/` | Low-level robot facades: `mobility`, `manipulation`, `head`. |
| `common/` | Cross-cutting leaf utilities: `logging`, `geometry`, `ros_services`, `script_paths`. |

## Two rules that keep this readable

**1. Dependency direction is one-way.**

```
nodes  ->  core  ->  {perception, navigation, skills, agents, inputs, comms, robot}  ->  common
```

A module never imports "upward" (e.g. `perception` never imports `core`).

**2. Pure logic is separated from ROS glue by file name, not by folder.**

Within a concept folder, intention-revealing names tell you which is which:
`pose.py` is pure math; `pose_tracking.py` is the tf2/odom adapter. The *pure*
files — `perception/pose`, `perception/image_codec`, `navigation/payload`,
`skills/registry`, `comms/messages`, `core/config` — import **no `rclpy`** and are
unit-tested without a ROS runtime. `common/import_rules_test.py` enforces this so
the boundary can't rot.

## Tests

Unit tests are co-located as `<name>_test.py` next to the module they cover (run the
pure ones with plain `pytest`, no ROS needed). Integration/launch tests live in the
package's top-level `test/` directory (wired into the ament build).
