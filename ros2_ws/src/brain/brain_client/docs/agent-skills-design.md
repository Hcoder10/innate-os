# Skill Identity & Directory Design

# Written by Karmanyaah

- A skill directory contains metadata.json.

## Physical Skills
### Behavior Server

Plays back trained skills

- Receives skill_dir

READ {METADATA.json}->.execution

READ nom. {SKILL_DIR}/{RUN_NUM}/**/*.{pt,pth}
(files specified in .execution)

### Training Server

- Receives skill_dir

READ metadata.json
RW .training_skill_id
WRITE .execution

READ {SKILL_DIR}/data/
WRITE/CREATE {SKILL_DIR}/{RUN_NUM}/

### Recorder Node

Record and play back stuff

READ/WRITE/CREATE {SKILL_DIR}/data/{dataset_metadata.json,episode_{NUM}.h5}

## Both Physical and Code Skills

### Skills Action Server

Handles execution of both physical skills and code skills.

### Brain Client

Uses names to send to LLM.

# Agent Written stuff

## Skill Types

There are two fundamentally different kinds of skills:

### Code Skills
A `.py` file in a skills directory. Contains a class inheriting from `Skill` with `name`, `execute()`, and `cancel()`. Runs in-process inside skills_action_server. Written by developers.

### Physical Skills
A subdirectory containing `metadata.json`. Involve the robot's arm/body. Have a lifecycle:

1. **Creation** (skills_action_server) — app calls `create_physical_skill` with a display name. Server creates the directory under `~/skills/` with a kebab-case name and writes `metadata.json`.
2. **Recording** (recorder_node) — human demonstrates by puppeteering the arm. Recorder is activated with an absolute directory path and creates episode H5 files + `dataset_metadata.json` under `data/`.
3. **Training** (training_node → cloud) — episode data uploaded, neural network trained, checkpoint downloaded back.
4. **Execution** (skills_action_server → behavior_server) — runs the trained policy or replays recorded motion.

Physical skill subtypes:
- **Learned** (`"type": "learned"`) — ACT neural network policy inference
- **Replay** (`"type": "replay"`) — plays back a recorded H5 file's actions

### Directory structure of a physical skill
```
pick-socks/
├── metadata.json                    ← skill definition (name, type, execution config) — created by skills_action_server
└── data/
    ├── dataset_metadata.json        ← recording metadata (episode count, timestamps) — created by recorder_node
    ├── episode_0.h5
    └── episode_1.h5
```

### Ownership boundaries
- **skills_action_server** owns `metadata.json` (creation, reading for skill discovery)
- **recorder_node** owns `data/` subdirectory (`dataset_metadata.json` + episode H5 files)
- **training_node** owns run output directories and `.execution`/`.training_skill_id` in metadata

### Execution call chain
```
brain_client (LLM) → /execute_skill → skills_action_server
                                           │
                                 ┌─────────┴─────────┐
                           Code skill?          Physical skill?
                           Run execute()        Forward to behavior_server
                           in-process           via /behavior/execute
                                                     │
                                           ┌─────────┴─────────┐
                                       Learned?            Replay?
                                       Run ACT policy      Play H5 actions
```

### Physical skill creation flow (app → robot)
```
App (CreateSkillScreen)
  │
  ├─ 1. callROSService(/brain/create_physical_skill, { name: "Pick Socks" })
  │     → skills_action_server creates ~/skills/pick-socks/metadata.json
  │     → returns { success, skill_directory: "/home/user/skills/pick-socks" }
  │
  └─ 2. callROSService(/brain/recorder/activate_physical_primitive, { task_directory: "/home/user/skills/pick-socks" })
        → recorder_node activates recording for that directory
```

---

## Skill ID Format

Skill IDs follow the pattern: `<user>/<skill_name>` matching `[a-z0-9_-]+/[a-z0-9_-]+`.

- **Code skill ID**: `<user>/<filename_without_py>` (e.g. `innate-os/navigate_to_position`)
- **Physical skill ID**: `<user>/<directory_name>` (e.g. `local/pick-socks`)

### Character rules
- Allowed characters in both `<user>` and `<skill_name>`: `[a-z0-9_-]`
- All inputs must be stripped of leading/trailing whitespace
- When creating a skill:
  - Either `name` (display name) or `id` must be provided
  - If `name` is not provided → `name = skill_name` portion of the ID
  - If `id` is not provided → `skill_name` derived from name: spaces → dashes, all other special chars removed, uppercase → lowercase
  - `id` must start with `innate-os/` or `local/`
- The `name` field in `metadata.json` is the human-readable display name (free-form text, not constrained)
- The directory name on disk **is** the `<skill_name>` portion of the ID

### Special users

| User | Disk location | Description |
|------|--------------|-------------|
| `local` | `~/skills/<skill_name>/` | User-created skills (recording, custom code) |
| `innate-os` | `$INNATE_OS_ROOT/extensions/skills/<skill_name>/` | Built-in skills shipped with innate-os |

No other users are valid yet. Future: cloud-synced skills from other users.

### ID ↔ Path resolution

```
innate-os/<skill_name>  →  $INNATE_OS_ROOT/extensions/skills/<skill_name>/
local/<skill_name>   →  ~/skills/<skill_name>/
```

For code skills, the "directory" is the parent directory containing the `.py` file:
```
innate-os/navigate_to_position  →  $INNATE_OS_ROOT/extensions/skills/navigate_to_position.py
local/my-custom-skill        →  ~/skills/my-custom-skill.py
```

### Examples

| ID | Display Name | Path |
|----|-------------|------|
| `local/pick-socks` | Pick Socks | `~/skills/pick-socks/` |
| `innate-os/wave` | wave | `$INNATE_OS_ROOT/extensions/skills/wave/` |
| `innate-os/navigate_to_position` | navigate_to_position | `$INNATE_OS_ROOT/extensions/skills/navigate_to_position.py` |
| `local/arm-circle` | Arm Circle | `~/skills/arm-circle/` |

---

## Interface Decisions

### Actions

| Interface | Type | Route | Param | Format | Notes |
|-----------|------|-------|-------|--------|-------|
| `/execute_skill` | `ExecuteSkill` | brain_client → skills_action_server | `skill_type` | **ID** (`user/name`) | Field is named `skill_type` but values are skill IDs. skills_action_server resolves to path internally |
| `/behavior/execute` | `ExecuteBehavior` | skills_action_server → behavior_server | `skill_dir` | **Absolute path** | Internal interface — skills_action_server resolves the path and passes it through |

### Services — Skill Creation & Discovery

| Interface | Type | Server | Param | Format | Notes |
|-----------|------|--------|-------|--------|-------|
| `/brain/create_physical_skill` | `CreatePhysicalSkill` | skills_action_server | `name` | Display name | Creates dir + `metadata.json` under `~/skills/`. Returns `success`, `message`, `skill_directory` (abs path), `skill_id`. Converts display name to kebab-case for directory name. |
| `/brain/reload_primitives` | `Trigger` | skills_action_server | *(none)* | — | Reloads all skills from disk |
| `/brain/reload_skills` | `ReloadSkillsAgents` | skills_action_server | `skills[]` | **IDs** | Selectively reload specific skills by ID (empty list = reload all) |
| `/brain/reload_skills_agents` | `ReloadSkillsAgents` | brain_client | `skills[]` | **IDs** | Forwards to `/brain/reload_skills` |
| `/brain/reload` | `Trigger` | brain_client | *(none)* | — | Full reload of everything |

### Services — Recording (recorder_node)

| Interface | Type | Param | Format | Notes |
|-----------|------|-------|--------|-------|
| `brain/recorder/activate_physical_primitive` | `ActivateManipulationTask` | `task_directory` | **Absolute path** | Activates recording for an existing skill directory. Does NOT create the directory or `metadata.json` — that's done by `create_physical_skill` first. |
| `brain/recorder/get_task_metadata` | `GetTaskMetadata` | `task_directory` | **Absolute path** | Returns enriched dataset metadata (episodes, timesteps, etc.) from `dataset_metadata.json` |
| `brain/recorder/load_episode` | `LoadEpisode` | `task_directory`, `episode_id` | **Absolute path** + int | Loads an episode for replay |
| `brain/recorder/new_episode` | `Trigger` | *(none)* | — | Uses active task |
| `brain/recorder/save_episode` | `Trigger` | *(none)* | — | |
| `brain/recorder/cancel_episode` | `Trigger` | *(none)* | — | |
| `brain/recorder/stop_episode` | `Trigger` | *(none)* | — | |
| `brain/recorder/end_task` | `Trigger` | *(none)* | — | |
| `brain/recorder/play_replay` | `Trigger` | *(none)* | — | |
| `brain/recorder/pause_replay` | `Trigger` | *(none)* | — | |
| `brain/recorder/stop_replay` | `Trigger` | *(none)* | — | |

### Services — Cloud Training (innate_training node)

| Interface | Type | Param | Format | Notes |
|-----------|------|-------|--------|-------|
| `~/submit_skill` | `SubmitSkill` | `skill_dir` | **Absolute path** | Display name read from `metadata.json` |
| `~/create_run` | `CreateRun` | `skill_dir` | **Absolute path** | |
| `~/download_results` | `DownloadResults` | `skill_dir`, `run_id` | **Absolute path** + int | |

### Topics

| Topic | Type | Publisher | Fields | Notes |
|-------|------|----------|--------|-------|
| `/brain/available_skills` | `AvailableSkills` | skills_action_server | `SkillInfo[]` with `id`, `name`, `type`, `guidelines`, `inputs_json`, `in_training`, `episode_count`, `directory` | Latched (transient_local QoS) |
| `/brain/recorder/status` | `RecorderStatus` | recorder_node | `task_directory`, `episode_number`, `status` | `task_directory` is absolute path |
| `/brain/recorder/replay_status` | `ReplayStatus` | recorder_node | `task_directory`, `episode_id`, etc. | `task_directory` is absolute path |
| `~/job_statuses` | `TrainingJobList` | training_node | `training_skill_id`, `skill_name`, `skill_dir` | `training_skill_id` is the cloud server's UUID, separate from the local skill ID. `skill_dir` is absolute path |

### Internal interfaces (not on ROS)

| Interface | Where | Param | Notes |
|-----------|-------|-------|-------|
| Agent `get_skills()` | agent Python classes | Returns **IDs** | Agents declare which skills they can use by ID |
| `register_primitives_and_directive` | brain_client → websocket | Each primitive includes **id**, **name** | LLM sees both; uses ID in tool calls |
| Hot reload watcher | brain_client | Maps file changes to **IDs** | Derives `innate-os/<stem>` or `local/<stem>` based on which directory the file is in |