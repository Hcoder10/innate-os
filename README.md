<div align="center">

# Innate OS

**The open-source operating system for MARS and other teachable robots.**

[![Discord](https://img.shields.io/badge/Discord-Join%20our%20community-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/innate)
[![Documentation](https://img.shields.io/badge/Docs-Read%20the%20docs-blue?style=for-the-badge&logo=readthedocs&logoColor=white)](https://docs.innate.bot)
[![Website](https://img.shields.io/badge/Website-Visit%20us-orange?style=for-the-badge&logo=safari&logoColor=white)](https://innate.bot)
[![ROS 2](https://img.shields.io/badge/ROS%202-Humble-22314E?style=for-the-badge&logo=ros&logoColor=white)](https://docs.ros.org/en/humble/)

</div>

<p align="center">
  <img src="docs/assets/readme/innate-os-readme-hero.png" alt="Innate OS branded README launchpad for simulation, workspace behavior, and robot operation" width="100%">
</p>

MARS is a teachable robot: a new type of robot that you can teach to do what you want.

MARS is designed to get better as AI gets better. Innate OS is built around swappable model-driven components, including VLMs for observation and VLAs for action.

> What it is today is the worst it will ever be.

Innate OS is open source. If you made a robot compatible with it, [let us know](https://github.com/innate-inc/innate-os/issues) and we can reference it here.

<table>
  <tr>
    <td width="50%" valign="top"><strong>MARS</strong><br>First-class Innate OS robot.</td>
    <td width="50%" valign="top"><strong>Your robot</strong><br>Open an issue and we can add it here.</td>
  </tr>
</table>

<table>
  <tr>
    <td width="20%" valign="top"><a href="#skills"><strong>Skills</strong></a><br><sub>Teach actions.<br>Write code skills.<br>Trigger with one skill ID.</sub></td>
    <td width="20%" valign="top"><a href="#agents"><strong>Agents</strong></a><br><sub>Run autonomously.<br>Use Innate's harness.<br>Or create your own.</sub></td>
    <td width="20%" valign="top"><a href="#additional-inputs"><strong>Additional Inputs</strong></a><br><sub>Stream your data.<br>Add sensors.<br>Expose new components.</sub></td>
    <td width="20%" valign="top"><a href="#simulator"><strong>Simulator</strong></a><br><sub>Test without hardware.<br>Run MARS locally.<br>Try skills and agents.</sub></td>
    <td width="20%" valign="top"><a href="#ros-reference"><strong>ROS Reference</strong></a><br><sub>Work below the high-level API.<br>Inspect ROS 2 packages.<br>Change core services.</sub></td>
  </tr>
</table>

<table>
  <tr>
    <td width="33%" align="center" valign="top">
      <img src="docs/assets/readme/screenshot-simulator-card.png" alt="Innate simulator interface" width="100%"><br>
      <sub>Simulator</sub>
    </td>
    <td width="33%" align="center" valign="top">
      <img src="docs/assets/readme/screenshot-mobile-card.png" alt="Innate mobile app running an agent" width="100%"><br>
      <sub>Mobile app</sub>
    </td>
    <td width="33%" align="center" valign="top">
      <img src="docs/assets/readme/screenshot-training-card.png" alt="Innate Training Manager skills view" width="100%"><br>
      <sub>Training Manager</sub>
    </td>
  </tr>
</table>

## Skills

Skills are the core unit of action on MARS. A skill can be digital, like calling a tool or service, or physical, like navigating, waving, grasping, recording a demonstration, or executing a learned manipulation policy.

<table>
  <tr>
    <td width="33%" valign="top"><strong>Execute manually</strong><br>Run skills from the <code>innate</code> CLI.</td>
    <td width="33%" valign="top"><strong>Operate from apps</strong><br>Trigger skills through the web app or Innate mobile apps.</td>
    <td width="33%" valign="top"><strong>Run autonomously</strong><br>Let agents select and interrupt skills as the world changes.</td>
  </tr>
</table>

```bash
innate skill list
innate skill type innate-os/wave
innate skill run innate-os/wave
innate skill run local/my-skill @x=1 @name=alice
```

<table>
  <tr>
    <td width="33%" valign="top"><strong><a href="workspace/innate_skills/">workspace/innate_skills/</a></strong><br>Built-in skills shipped with Innate OS.</td>
    <td width="33%" valign="top"><strong><code>workspace/custom_skills/</code></strong><br>Your local, recorded, or trained skills. Gitignored and created automatically.</td>
    <td width="33%" valign="top"><strong><code>~/skills/</code></strong><br>Optional personal skills directory, scanned in place when it exists.</td>
  </tr>
</table>

Skill IDs show where a skill came from:

```text
innate-os/wave      # built-in skill
local/my-skill      # local custom skill
```

Physical skills are recorded under `workspace/custom_skills/`. The Training Manager can help with skills, datasets, training runs, logs, and downloads.

## Agents

Agents allow Innate robots to run autonomously. They make the robot think in a high-frequency loop using a multimodal model, for example a VLM that is constantly observing the world.

An agent is usually a composition of:

- A set of skills the robot is allowed to use
- A system prompt that defines the robot's behavior
- A harness that connects the model to observations, memory, tools, and robot actions

You can use Innate's harness or bring your own. Multimodal robot agents have different constraints than purely digital agents: they need to observe continuously, run at a high enough frequency to react, and interrupt a running skill when the world has changed.

<table>
  <tr>
    <td width="33%" valign="top"><strong><a href="workspace/innate_agents/">workspace/innate_agents/</a></strong><br>Built-in agents shipped with Innate OS.</td>
    <td width="33%" valign="top"><strong><code>workspace/custom_agents/</code></strong><br>Your local agents. Gitignored and created automatically.</td>
    <td width="33%" valign="top"><strong><code>~/agents/</code></strong><br>Optional personal agents directory, scanned in place when it exists.</td>
  </tr>
</table>

Use the [simulator](#simulator) to test custom agents and custom harnesses before running them on a physical robot.

## Additional Inputs

Innate OS provides an SDK for streaming new data into running agents. Innate robots are designed to be naturally expandable: add a new sensor, expose it as an input device, and let agents request it by name.

Input devices live in [`workspace/inputs/`](workspace/inputs/) and are pure Python. They should not import ROS directly.

<details>
<summary>Thermometer input example</summary>

```python
# workspace/inputs/thermometer_input.py

import threading
import time

from brain_client.input_types import InputDevice


def read_thermometer_celsius() -> float:
    # Replace this with your hardware, websocket, serial, or API read.
    return 21.5


class ThermometerInput(InputDevice):
    def __init__(self, logger=None):
        super().__init__(logger)
        self._stop_event = threading.Event()
        self._thread = None

    @property
    def name(self) -> str:
        return "thermometer"

    def on_open(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def on_close(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def _loop(self):
        while not self._stop_event.is_set():
            self.send_data(
                {"celsius": read_thermometer_celsius(), "timestamp": time.time()},
                data_type="custom",
            )
            time.sleep(1.0)
```

An agent or directive can then request the input by name:

```python
def get_inputs(self):
    return ["thermometer"]
```

</details>

See [`docs/INPUT_DEVICES.md`](docs/INPUT_DEVICES.md) for the full input-device lifecycle.

## Simulator

Innate OS includes a high-level simulator running a replica of MARS. Use it to play with skills, agents, input devices, and your own agent harness before you have a robot on your desk.

```bash
./innate setup
./innate sim up
```

This starts the Docker-based Innate OS runtime, the simulator, and the built frontend at [http://localhost:8000](http://localhost:8000). The terminal opens a live dashboard with startup logs, simulator logs, brain logs, and runtime health.

```bash
./innate sim up --vis       # open the native simulator viewer
./innate sim status         # show current runtime state
./innate sim logs simulator # inspect simulator logs
./innate sim down           # stop the runtime
```

See [`sim/launcher/README.md`](sim/launcher/README.md) for the full local simulator workflow.

## ROS Reference

Innate OS is currently based on ROS 2, the reference framework for robotics operating systems. Most builders should start with skills, agents, inputs, and the simulator. Changing the core OS is not recommended for normal usage, but it is possible.

<table>
  <tr>
    <td width="20%" valign="top"><strong><a href="ros2_ws/">ros2_ws/</a></strong><br>Robot runtime workspace.</td>
    <td width="20%" valign="top"><strong><a href="docs/SYSTEM_OVERVIEW.md">System Overview</a></strong><br>Architecture reference.</td>
    <td width="20%" valign="top"><strong><a href="scripts/launch_ros_in_tmux.sh">Startup</a></strong><br>Robot node wiring.</td>
    <td width="20%" valign="top"><strong><a href="scripts/update/README.md">Updates</a></strong><br>Services and CLI commands.</td>
    <td width="20%" valign="top"><strong><a href="config/">config/</a></strong><br>DDS, systemd, udev, audio, Bluetooth, sounds, and shell setup.</td>
  </tr>
</table>

<details>
<summary>Main ROS 2 runtime packages</summary>

- **[maurice_control](ros2_ws/src/maurice_bot/maurice_control)** - top-level robot app node, rosbridge websocket server for the mobile/web app, and low-latency UDP receiver for leader-arm teleop.
- **[maurice_bringup](ros2_ws/src/maurice_bot/maurice_bringup)** - hardware bringup for motors, base, IMU, and LiDAR, plus `robot_state_publisher` for the TF tree.
- **[maurice_arm](ros2_ws/src/maurice_bot/maurice_arm)** - arm and head servo driver, MoveIt `move_group`, and KDL-based IK solver.
- **[maurice_cam](ros2_ws/src/maurice_bot/maurice_cam)** - stereo main camera, arm camera, VPI stereo depth estimator, WebRTC streamer, and stereo calibration action server.
- **[maurice_nav](ros2_ws/src/maurice_bot/maurice_nav)** - Nav2-based navigation, SLAM mapping, and the mode manager that switches between `mapfree`, `mapping`, and `navigation`.
- **[brain_client](ros2_ws/src/brain/brain_client)** - bridge to the Innate cloud brain, websocket client, skills action server, and user input manager.
- **[manipulation](ros2_ws/src/brain/manipulation)** - records and replays manipulation demonstrations and runs learned or scripted manipulation policies.
- **[innate_logger](ros2_ws/src/cloud/innate_logger)** - uploads robot logs and telemetry to the Innate cloud.
- **[innate_training_node](ros2_ws/src/cloud/innate_training_node)** - collects training episodes and pushes them to the training cloud.
- **[innate_uninavid](ros2_ws/src/cloud/innate_uninavid)** - UniNaVid vision-language navigation client.

</details>

## More Docs

- [`workspace/README.md`](workspace/README.md) - where agents and skills live.
- [`ros2_ws/src/brain/brain_client/docs/agent-skills-design.md`](ros2_ws/src/brain/brain_client/docs/agent-skills-design.md) - agent and skill loading model.
- [`sim/launcher/README.md`](sim/launcher/README.md) - full `./innate sim` workflow.
- [`docs/INPUT_DEVICES.md`](docs/INPUT_DEVICES.md) - how to add input devices.
- [`scripts/update/README.md`](scripts/update/README.md) - updates, services, and skill CLI commands.
