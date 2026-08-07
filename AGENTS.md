# Innate OS — Agent Reference

## What is Innate?

Innate OS is a lightweight, agentic operating system for the **MARS** — a small mobile robot with an arm.
It runs on an **NVIDIA Jetson Orin Nano 8GB** (resource-constrained by design) and boots the full stack in under a minute.

The OS is built on **ROS 2 (Humble)** with **Zenoh** as the DDS transport, and natively supports agentic workflows, vision-language navigation (VLN), and vision-language-action (VLA) models via an on-robot agent loop that calls a hosted vision-language model.

For full documentation and hardware details, see [innate.bot](https://www.innate.bot/) and [docs.innate.bot](https://docs.innate.bot).

## System Modes

| Mode | Description |
|---|---|
| `hardware` | Running on the physical MARS robot |
| `sim` | Running in the software simulator (same ROS nodes, mock sensors) |

## CLI — `innate`

Run `innate` with no arguments to print the current system status (version, mode, ROS/DDS state).

| Command | Description |
|---|---|
| `innate view` | Attach to running nodes |
| `innate restart` | Restart ROS nodes |
| `innate build` | Build and restart |
| `innate build release` | Build release mode and restart |
| `innate skill list` | List available robot skills |
| `innate update` | Check and apply updates |
| `innate diag` | Check hardware diagnostics |
| `innate volume` | Get or set speaker volume |
| `innate --help` | Show all commands |

## Writing Skills

Skills live in `workspace/` (see [workspace/README.md](workspace/README.md)). A skill is a
`Skill` subclass; everything it consumes is declared with a type annotation.

### Never `time.sleep` — always `self.sleep`

**In skill code, use `self.sleep(seconds)`. Never `time.sleep(seconds)`.**

`self.sleep` wakes and raises `SkillCancelled` the moment a Stop lands; `time.sleep` blocks
to completion, so a skill that uses it keeps running (and keeps the robot moving) after the
user pressed Stop. Sleeping is the only cancel point a loop needs — write the loop as if
cancel didn't exist and let the framework halt the base and report `CANCELLED`.

```python
while traveled < target:
    self.mobility.send_cmd_vel(linear_x=velocity, duration=0.5)
    self.sleep(0.1)          # ✅ cancellable
    # time.sleep(0.1)        # ❌ Stop is ignored until the sleep finishes
```

`time` itself is fine for *measuring* — `time.time()` / `time.monotonic()` for deadlines and
elapsed checks. The rule is only about blocking.

Related cancel-aware helpers, all of which raise `SkillCancelled` too:

| Call | Use for |
|---|---|
| `self.sleep(seconds)` | Any pause in skill code |
| `self.wait_for(read, timeout)` | Block until a reader returns non-`None` |
| `self.check_cancelled()` | A checkpoint with no sleep (e.g. before an irreversible commit) |
| `self.cancelled` | Read the latch without raising |

Cleanup belongs in `try/finally` inside `execute()`. `self.on_cancel(hook)` is only for
forwarding a cancel to an external action goal — braking the base is automatic.

### The one exception: committed, non-cancellable sections

Teardown and already-committed physical actions must **not** be cancellable, so they use
`time.sleep` on purpose. Once `pick_any_object` closes the gripper, a cancel must not unwind
mid-grip and drop the object over the floor, so `_close_twist_lift` sleeps with `time.sleep`
and the run finishes carrying the object home.

If you write such a section, say so in a comment — otherwise the next reader "fixes" it back
to `self.sleep` and reintroduces the bug. Everywhere else, `self.sleep`.

## Code Style

Write code that reads like prose — structure carries the meaning; comments are a last resort
(see [CLAUDE.md](CLAUDE.md#comments-minimal-why-only) for the full comments rule). The best
part is no part: prefer deleting or reusing over adding.

### Shape

- **Early returns, minimal indentation.** Guard clauses over nested ifs; merge related
  conditions with `and`/`or`. A multi-clause condition worth explaining becomes a small named
  predicate method (`_abandon(turn)`), not a comment.
- **One function, one altitude.** Split do-everything functions into named steps so the caller
  reads as a sentence — e.g. the agent loop: `_turn` owns only the error boundary, `_think` is
  the straight-line look → generate → commit → act, `_generate` owns the one blocking call.
- **One concern per module.** When a file grows a second job, split it (gemini.py became
  transport.py / tools.py / context.py). Pure functions — no ROS, no I/O, no self-state — live
  in a `utils.py` beside their caller.

### Types

- Every signature fully typed. Collaborator classes are imported under `if TYPE_CHECKING:` —
  annotations must add zero runtime import edges.
- Pick the representation by where the data lives:
  - **Frozen `@dataclass`** for process-internal domain objects (`Event`, `RunningSkill`).
  - **`TypedDict`** only when the dict *is* the wire format — parsed from JSON, republished as
    JSON, or handed to workspace user code (`SkillMeta`). Foreign API bodies stay plain dicts.
  - **StrEnum** (`brain_client/common/enums.py` backport; py3.10 has no `enum.StrEnum`) for
    closed string vocabularies: event kinds, trace names, chat senders, backends. The values
    are wire-visible — never change them. Keys of external APIs stay string literals.
- Name the shapes you pass around (`Transport = Callable[[str, dict], Iterator[dict]]`,
  `Frame = tuple[FrameLabel, bytes]`); reuse an existing alias (`Pose`) instead of redefining it.
- `basedpyright` stays at **0 errors** on `brain_client`. Fix at the root — type the state
  field, thread a value through instead of re-reading a nullable attribute, make the base class
  generic, restructure so the narrow is visible — never `# type: ignore`. A `# noqa` needs its
  reason on the same line.

### try/except

- A `try` is an invariant at a boundary, not insurance. Legitimate boundaries: a long-lived
  loop's crash reporter, ROS executor callbacks, user code (skills, agents, input devices),
  import machinery, and `finally` for cleanup/flags.
- Catch narrow types for parses (`json.JSONDecodeError`, `KeyError`, `ValueError`). Never wrap
  code that cannot raise; guard only the statement that can.
- In a loop over independent items, catch per item — one bad device/skill/agent must not stop
  the rest.
- Broad `except Exception` carries its justification: `# noqa: BLE001 — one bad agent must not
  stop the roster`.

### Before / after

The rules above, compressed into one transformation (dict-shaped state, nested ifs,
narrating comments, blanket try — versus typed, flat, silent):

```python
# ❌ Before
def _report_result(self, result, running):
    # handle the result of the skill
    try:
        if running is not None:
            # check that the ids match
            if running.get("skill_id") == result.skill_type:
                if result.success:
                    output = result.message
                    # send the output to the chat
                    if output is not None and output.strip() != "":
                        self._chat.emit("skill_output", output, speak=False)
                    self.on_event("completed", running.get("primitive_name", "unknown"))
                else:
                    self.on_event("failed", running.get("primitive_name", "unknown"), result.message)
    except Exception as e:
        self._logger.error(f"Error reporting result: {e}")
```

```python
# ✅ After
def _report_result(self, result: ExecuteSkill.Result, running: RunningSkill | None) -> None:
    if running is None or running.skill_id != result.skill_type:
        return  # a stale result must not report against a newer run
    if not result.success:
        self.on_event("failed", running.primitive_name, result.message)
        return
    if result.message.strip():
        self._chat.emit(Sender.SKILL_OUTPUT, result.message, speak=False)
    self.on_event("completed", running.primitive_name)
```

What changed, rule by rule: the signature is fully typed, so `running.get("skill_id")`
becomes `running.skill_id` and the `"unknown"` fallback vanishes — the type guarantees the
field. Four nesting levels become guard clauses with merged conditions. The magic string
`"skill_output"` becomes `Sender.SKILL_OUTPUT`. Every narrating comment is deleted; the one
that survives states an invariant the code can't show. The blanket `try` is gone — nothing
here can raise, and the ROS callback boundary that *calls* this already owns crash reporting.

### Done means verified

Before reporting work complete, all three must be clean:

```bash
ruff check ros2_ws/src/brain/brain_client/ && ruff format --check ros2_ws/src/brain/brain_client/
```

```bash
cd ros2_ws/src/brain/brain_client && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest test/ -q --ignore=test/test_node_boot.launch.py
```

```bash
cd ros2_ws/src/brain/brain_client && basedpyright brain_client/
```

## Key ROS Packages

| Package | Role |
|---|---|
| `mars_control` | Top-level app node, rosbridge websocket, teleop receiver |
| `mars_bringup` | Motor/IMU/LiDAR hardware drivers, TF tree |
| `mars_arm` | Arm + head servos, IK solver |
| `mars_cam` | Stereo cameras, depth estimation, WebRTC stream |
| `mars_nav` | Nav2 navigation, SLAM, mode manager |
| `brain_client` | Cloud brain bridge (STT/TTS, skills action server) |
| `manipulation` | Records/replays and runs manipulation policies |
