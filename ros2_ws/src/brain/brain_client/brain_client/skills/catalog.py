"""Skill catalog: discovery, metadata, publishing, reload, and physical skills.

Owns the loaded code/physical/in-training skill dicts (guarded by a lock against
the hot-reload thread), the latched ``/brain/available_skills`` publisher, the
skill cache, and the hot-reload watcher. The executor asks this for skills via the
thread-safe getters; the node's reload/create services delegate here.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import threading
import time
import types
from pathlib import Path
from typing import Literal, get_args, get_origin

from brain_messages.msg import AvailableSkills, SkillInfo
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy

from brain_client.common.script_paths import (
    classify_source,
    ensure_user_directories,
    get_custom_skills_dir,
    get_innate_skills_dir,
    get_skill_directories,
)
from brain_client.skills.hot_reload_watcher import HotReloadWatcher
from brain_client.skills.loader import SkillLoader
from brain_client.skills.types import Skill


class SkillRepository:
    def __init__(self, node, *, interface_injector, simulator_mode: bool):
        self._node = node
        self._logger = node.get_logger()
        self._inject = interface_injector
        self.simulator_mode = simulator_mode

        self.skill_loader = SkillLoader(self._logger)
        self._skills_directories = self._resolve_skills_directories()

        # Guards _code_skills / _physical_skills / _in_training_skills against the
        # HotReloadWatcher background thread.
        self._skills_lock = threading.Lock()
        self._code_skills: dict[str, tuple[str, Skill]] = {}  # {id: (display_name, instance)}
        self._physical_skills: dict[str, dict] = {}
        self._in_training_skills: dict[str, dict] = {}

        self._code_skills = self._load_code_skills(self._skills_directories)
        self._logger.info(f"Successfully loaded {len(self._code_skills)} code skills")
        self._physical_skills, self._in_training_skills = self._load_physical_skills(self._skills_directories)
        self._logger.info(f"Successfully loaded {len(self._physical_skills)} physical skills")
        self._logger.info(f"Found {len(self._in_training_skills)} in-training skills")

        qos = QoSProfile(
            depth=1,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
            reliability=QoSReliabilityPolicy.RELIABLE,
        )
        self._skills_publisher = node.create_publisher(AvailableSkills, "/brain/available_skills", qos)

        self._hot_reload_watcher = None

    # --- counts / thread-safe accessors (used by executor + service messages) ---
    @property
    def code_count(self) -> int:
        return len(self._code_skills)

    @property
    def physical_count(self) -> int:
        return len(self._physical_skills)

    def get_code_skill(self, skill_id: str):
        with self._skills_lock:
            return self._code_skills.get(skill_id)

    def get_physical_skill(self, skill_id: str):
        with self._skills_lock:
            return self._physical_skills.get(skill_id)

    def all_skill_ids(self) -> list[str]:
        with self._skills_lock:
            return list(self._code_skills.keys()) + list(self._physical_skills.keys())

    def all_code_skills(self) -> list[tuple[str, tuple[str, Skill]]]:
        with self._skills_lock:
            return list(self._code_skills.items())

    # --- watcher ---
    def start_watcher(self) -> None:
        self._hot_reload_watcher = HotReloadWatcher(
            logger=self._logger,
            skills_directories=self._skills_directories,
            agents_directories=[],  # SAS doesn't handle agents
            on_reload=self._on_skills_file_changed,
            debounce_seconds=1.0,
            recursive=True,  # physical skills live in subdirs (metadata.json + assets)
        )
        self._hot_reload_watcher.start()

    def stop_watcher(self) -> None:
        if self._hot_reload_watcher is not None:
            self._hot_reload_watcher.stop()

    def _on_skills_file_changed(self, skill_names: list, _agent_names: list) -> None:
        """Called by HotReloadWatcher when skill files change.

        Names are code-skill stems (``foo.py`` -> ``foo``) or physical-skill
        directory names (``foo/metadata.json`` -> ``foo``); both resolve below.
        """
        self._logger.info(f"Hot reload triggered for skills: {skill_names}")
        if not skill_names:
            self.reload_all()
            return
        skill_ids = []
        for stem in skill_names:
            for d in self._skills_directories:
                py_file = Path(d) / f"{stem}.py"
                subdir = Path(d) / stem
                if py_file.exists():
                    skill_ids.append(self._compute_skill_id(py_file))
                    break
                elif subdir.is_dir():
                    skill_ids.append(self._compute_skill_id(subdir))
                    break
        self.reload_selective(skill_ids)

    # --- loading ---
    def _load_code_skills(self, skills_directories) -> dict[str, tuple[str, Skill]]:
        discovered_skills = self.skill_loader.load_from_directories(skills_directories)
        self._logger.info(f"Discovered skills: {list(discovered_skills.keys())} in directories {skills_directories}")

        id_keyed: dict[str, tuple[str, type, Path]] = {}
        for display_name, (cls, src_path) in discovered_skills.items():
            id_keyed[self._compute_skill_id(src_path)] = (display_name, cls, src_path)
        self._apply_sim_swap(id_keyed)

        code_skills: dict[str, tuple[str, Skill]] = {}
        for skill_id, (display_name, skill_class, src_path) in id_keyed.items():
            try:
                instance = self._instantiate(skill_class, src_path)
                code_skills[skill_id] = (display_name, instance)
                self._logger.info(f"Loaded code skill: {skill_id} ({display_name}) [source={instance.source}]")
            except Exception as e:
                self._logger.error(f"Error instantiating skill {skill_id}: {e}")
        return code_skills

    def _instantiate(self, skill_class, src_path):
        instance = skill_class(self._logger)
        instance.node = self._node
        instance.source = classify_source(src_path)
        self._inject(instance)
        return instance

    def _load_physical_skills(self, skills_directories):
        physical_skills = {}
        in_training_skills = {}
        for skills_directory in skills_directories:
            if not os.path.exists(skills_directory):
                continue
            for item in os.listdir(skills_directory):
                item_path = os.path.join(skills_directory, item)
                if not os.path.isdir(item_path):
                    continue
                metadata_path = os.path.join(item_path, "metadata.json")
                if not os.path.exists(metadata_path):
                    continue
                try:
                    with open(metadata_path) as f:
                        metadata = json.load(f)
                    if not isinstance(metadata, dict):
                        self._logger.warn(
                            f"Skipped {metadata_path}: top-level JSON is {type(metadata).__name__}, expected object"
                        )
                        continue
                    skill_id = self._compute_skill_id(Path(item_path))
                    is_valid, is_in_training, episode_count = self.skill_loader.validate_physical_skill(
                        item_path, metadata
                    )
                    if not is_valid:
                        self._logger.warn(f"Skipped invalid physical skill: {skill_id}")
                        continue
                    skill_data = {
                        "metadata": metadata,
                        "directory": item_path,
                        "in_training": is_in_training,
                        "episode_count": episode_count,
                    }
                    if is_in_training:
                        in_training_skills[skill_id] = skill_data
                        self._logger.info(
                            f"Loaded in-training skill: {skill_id} (type: {metadata.get('type', 'unknown')})"
                        )
                    else:
                        physical_skills[skill_id] = skill_data
                        self._logger.info(
                            f"Loaded physical skill: {skill_id} (type: {metadata.get('type', 'unknown')})"
                        )
                except json.JSONDecodeError as e:
                    self._logger.error(f"Skipped {metadata_path}: invalid JSON ({e})")
                except OSError as e:
                    self._logger.error(f"Skipped {metadata_path}: read failed ({e})")
                except Exception as e:
                    self._logger.error(f"Skipped physical skill at {item_path}: {e}")
        return physical_skills, in_training_skills

    def _resolve_skills_directories(self) -> list[str]:
        innate_skills_dir = str(get_innate_skills_dir())
        if not os.path.exists(innate_skills_dir):
            self._logger.fatal(f"Skills directory not found: {innate_skills_dir}")
            raise FileNotFoundError(f"Skills directory must exist at {innate_skills_dir}")
        ensure_user_directories()
        directories = [str(p) for p in get_skill_directories()]
        for directory in directories:
            self._logger.info(f"Scanning skills directory: {directory}")
        return directories

    def _apply_sim_swap(self, id_keyed: dict[str, tuple[str, type, Path]]) -> None:
        """Swap navigate_to_position_sim -> navigate_to_position in sim mode, or remove it."""
        sim_id = "innate-os/navigate_to_position_sim"
        real_id = "innate-os/navigate_to_position"
        if self.simulator_mode and sim_id in id_keyed:
            self._logger.info("Simulator mode: using NavigateToPositionSim for navigate_to_position")
            _name, cls, src = id_keyed.pop(sim_id)
            id_keyed[real_id] = ("navigate_to_position", cls, src)
        elif sim_id in id_keyed:
            self._logger.info("Real robot mode: removing sim navigation skill")
            del id_keyed[sim_id]

    def _compute_skill_id(self, path: str | Path) -> str:
        path_str = str(Path(path))
        basename = Path(path_str).stem if path_str.endswith(".py") else Path(path_str).name
        prefix = "innate-os" if classify_source(path) == "shipped" else "local"
        return f"{prefix}/{basename}"

    # --- reload ---
    def reload_all(self) -> None:
        self._logger.info("Reloading skills...")
        self._skills_directories = self._resolve_skills_directories()
        new_code_skills = self._load_code_skills(self._skills_directories)
        new_physical, new_in_training = self._load_physical_skills(self._skills_directories)
        with self._skills_lock:
            self._code_skills = new_code_skills
            self._physical_skills = new_physical
            self._in_training_skills = new_in_training
        self._logger.info(f"Reloaded {len(new_code_skills)} code + {len(new_physical)} physical skills")
        self.publish_skills_list()

    def reload_selective(self, skill_ids: list[str]) -> list[str]:
        """Reload specific skills by ID. Empty list means reload all."""
        if not skill_ids:
            self.reload_all()
            with self._skills_lock:
                return list(self._code_skills.keys()) + list(self._physical_skills.keys())

        self._logger.info(f"Selectively reloading skills: {skill_ids}")
        reloaded = []
        for skill_id in skill_ids:
            basename = skill_id.split("/", 1)[-1] if "/" in skill_id else skill_id
            with self._skills_lock:
                is_code = skill_id in self._code_skills or self._is_code_skill_id(skill_id)
                is_physical = (not is_code) and (
                    skill_id in self._physical_skills or skill_id in self._in_training_skills
                )
            if is_code:
                result = self.skill_loader.reload_skill_by_file_stem(basename, self._skills_directories)
                if result is not None:
                    cls, src_path = result
                    display_name = self.skill_loader._get_name(cls)
                    try:
                        instance = self._instantiate(cls, src_path)
                        with self._skills_lock:
                            self._code_skills[skill_id] = (display_name, instance)
                        reloaded.append(skill_id)
                        self._logger.info(f"Reloaded code skill: {skill_id}")
                    except Exception as e:
                        self._logger.error(f"Error instantiating {skill_id}: {e}")
            elif is_physical:
                if self._reload_physical_skill(skill_id):
                    reloaded.append(skill_id)
        self._logger.info(f"Selectively reloaded {len(reloaded)} skills")
        self.publish_skills_list()
        return reloaded

    def _is_code_skill_id(self, skill_id: str) -> bool:
        basename = skill_id.split("/", 1)[-1] if "/" in skill_id else skill_id
        return any((Path(d) / f"{basename}.py").exists() for d in self._skills_directories)

    def _reload_physical_skill(self, skill_id: str) -> bool:
        basename = skill_id.split("/", 1)[-1] if "/" in skill_id else skill_id
        for skills_directory in self._skills_directories:
            skill_path = os.path.join(skills_directory, basename)
            metadata_path = os.path.join(skill_path, "metadata.json")
            if os.path.exists(metadata_path):
                try:
                    with open(metadata_path) as f:
                        metadata = json.load(f)
                    is_valid, is_in_training, episode_count = self.skill_loader.validate_physical_skill(
                        skill_path, metadata
                    )
                    if is_valid:
                        skill_data = {
                            "metadata": metadata,
                            "directory": skill_path,
                            "in_training": is_in_training,
                            "episode_count": episode_count,
                        }
                        with self._skills_lock:
                            if is_in_training:
                                self._in_training_skills[skill_id] = skill_data
                                self._physical_skills.pop(skill_id, None)
                            else:
                                self._physical_skills[skill_id] = skill_data
                                self._in_training_skills.pop(skill_id, None)
                        self._logger.info(f"Reloaded physical skill: {skill_id}")
                        return True
                except Exception as e:
                    self._logger.error(f"Error reloading physical skill {skill_id}: {e}")
        return False

    def create_physical_skill(self, display_name: str) -> tuple[bool, str, str, str]:
        """Create a learned-skill directory with metadata.json. Returns (ok, msg, dir, id)."""
        try:
            display_name = display_name.strip()
            if not display_name:
                return False, "Skill name cannot be empty.", "", ""

            dir_name = re.sub(r"[^a-zA-Z0-9\s-]", "", display_name)
            dir_name = re.sub(r"\s+", "-", dir_name).strip("-").lower()
            if not dir_name:
                return False, f"Cannot derive valid directory name from '{display_name}'.", "", ""

            skill_dir = os.path.join(str(get_custom_skills_dir()), dir_name)
            if os.path.exists(os.path.join(skill_dir, "metadata.json")):
                self._logger.info(f"Skill '{display_name}' already exists at {skill_dir}. Returning existing.")
                return True, f"Skill already exists at {skill_dir}.", skill_dir, self._compute_skill_id(skill_dir)

            os.makedirs(skill_dir, exist_ok=True)
            metadata = {
                "name": display_name,
                "type": "learned",
                "guidelines": "",
                "guidelines_when_running": "",
                "inputs": {},
                "execution": {
                    "duration": None,
                    "progress_threshold": None,
                    "start_pose": None,
                    "end_pose": None,
                    "n_action_steps": None,
                },
            }
            metadata_path = os.path.join(skill_dir, "metadata.json")
            tmp_path = metadata_path + ".tmp"
            try:
                with open(tmp_path, "w") as f:
                    json.dump(metadata, f, indent=4)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, metadata_path)
            except Exception:
                try:
                    os.remove(tmp_path)
                except (FileNotFoundError, OSError):
                    pass
                raise

            self._logger.info(f"Created physical skill '{display_name}' at {skill_dir}")
            self.reload_all()
            return True, f"Created skill '{display_name}' at {skill_dir}.", skill_dir, self._compute_skill_id(skill_dir)
        except Exception as e:
            self._logger.error(f"Error creating physical skill: {e}")
            return False, f"Failed to create skill: {e}", "", ""

    # --- publishing ---
    def publish_skills_list(self) -> None:
        """Build and publish the full AvailableSkills message on the latched topic."""
        msg = AvailableSkills()
        skills = []

        with self._skills_lock:
            code_skills_snapshot = dict(self._code_skills)
            physical_skills_snapshot = dict(self._physical_skills)
            in_training_skills_snapshot = dict(self._in_training_skills)

        for skill_id, (display_name, skill_instance) in code_skills_snapshot.items():
            try:
                inputs = self._inspect_skill_inputs(skill_id, skill_instance)
                guidelines = self._safe_skill_string(skill_id, skill_instance, "guidelines")
                guidelines_when_running = self._safe_skill_string(skill_id, skill_instance, "guidelines_when_running")
                try:
                    inputs_json = json.dumps(inputs)
                except (TypeError, ValueError) as e:
                    self._logger.error(f"Could not serialize inputs for code skill '{skill_id}': {e}; using empty")
                    inputs_json = "{}"
                skills.append(
                    self._build_skill_info(
                        skill_id=skill_id,
                        name=display_name,
                        skill_type="code",
                        guidelines=guidelines,
                        guidelines_when_running=guidelines_when_running,
                        inputs_json=inputs_json,
                    )
                )
            except Exception as e:
                self._logger.error(f"Skipping code skill '{skill_id}' in available_skills: {e}")
                continue

        for skill_id, physical_data in physical_skills_snapshot.items():
            try:
                info = self._build_physical_skill_info(skill_id, physical_data, in_training=False)
                if info is not None:
                    skills.append(info)
            except Exception as e:
                self._logger.error(f"Skipping physical skill '{skill_id}' in available_skills: {e}")
                continue

        for skill_id, physical_data in in_training_skills_snapshot.items():
            try:
                info = self._build_physical_skill_info(skill_id, physical_data, in_training=True)
                if info is not None:
                    skills.append(info)
            except Exception as e:
                self._logger.error(f"Skipping in-training skill '{skill_id}' in available_skills: {e}")
                continue

        # Enforce unique display names (the LLM can't disambiguate duplicates).
        filtered_skills = []
        seen_names: dict[str, str] = {}
        for s in skills:
            if s.name in seen_names:
                self._logger.error(
                    f"DUPLICATE skill name '{s.name}' between {seen_names[s.name]} and {s.id}. "
                    f"Skipping '{s.id}' — rename the skill to fix this."
                )
                continue
            seen_names[s.name] = s.id
            filtered_skills.append(s)

        msg.skills = filtered_skills
        try:
            self._skills_publisher.publish(msg)
            self._write_skill_cache(filtered_skills)
            self._logger.info(f"Published {len(filtered_skills)} skills on /brain/available_skills")
        except Exception as e:
            self._logger.error(f"Failed to publish AvailableSkills (had {len(filtered_skills)} entries): {e}")

    def _build_skill_info(
        self,
        skill_id: str,
        name: str,
        skill_type: str,
        guidelines: str,
        guidelines_when_running: str,
        inputs_json: str,
        in_training: bool = False,
        episode_count: int = 0,
        directory: str = "",
    ) -> SkillInfo:
        msg = SkillInfo()
        msg.id = skill_id or ""
        msg.name = name or ""
        msg.type = skill_type or ""
        msg.guidelines = guidelines or ""
        msg.guidelines_when_running = guidelines_when_running or ""
        msg.inputs_json = inputs_json or ""
        msg.in_training = bool(in_training)
        msg.episode_count = int(episode_count or 0)
        msg.directory = directory or ""
        return msg

    def _inspect_skill_inputs(self, skill_id: str, skill_instance) -> dict:
        """Best-effort introspection of a code skill's execute() signature."""
        if not hasattr(skill_instance, "execute"):
            return {}
        try:
            signature = inspect.signature(skill_instance.execute)
        except (TypeError, ValueError) as e:
            self._logger.warn(f"Could not inspect execute() signature for '{skill_id}': {e}")
            return {}

        inputs: dict = {}
        for param_name, param in signature.parameters.items():
            if param_name == "self":
                continue
            param_type = "any"
            enum_values = None
            try:
                if param.annotation != inspect.Parameter.empty:
                    origin = get_origin(param.annotation)
                    if origin is Literal:
                        values = list(get_args(param.annotation))
                        value_type_names = {"None" if value is None else type(value).__name__ for value in values}
                        param_type = value_type_names.pop() if len(value_type_names) == 1 else "literal"
                        enum_values = values
                    elif (
                        isinstance(param.annotation, (types.UnionType, types.GenericAlias))
                        or hasattr(param.annotation, "_name")
                        and param.annotation._name in ["List", "Optional", "Dict", "Tuple", "Union"]
                    ):
                        param_type = str(param.annotation)
                    elif hasattr(param.annotation, "__name__"):
                        param_type = param.annotation.__name__
                    else:
                        param_type = str(param.annotation)
                    if isinstance(param_type, str):
                        param_type = param_type.replace("typing.", "")
            except Exception as e:
                self._logger.warn(f"Could not stringify annotation for '{skill_id}.{param_name}': {e}")
                param_type = "any"

            param_schema = {"type": param_type, "required": param.default == inspect.Parameter.empty}
            if enum_values is not None:
                param_schema["enum"] = enum_values
            if param.default != inspect.Parameter.empty:
                try:
                    json.dumps(param.default)
                    param_schema["default"] = param.default
                except (TypeError, ValueError):
                    pass
            inputs[param_name] = param_schema
        return inputs

    def _safe_skill_string(self, skill_id: str, skill_instance, attr: str) -> str:
        if not hasattr(skill_instance, attr):
            return ""
        try:
            value = getattr(skill_instance, attr)()
        except Exception as e:
            self._logger.warn(f"Skill '{skill_id}'.{attr}() raised: {e}; defaulting to empty string")
            return ""
        return str(value) if value is not None else ""

    def _build_physical_skill_info(self, skill_id: str, physical_data: dict, *, in_training: bool) -> SkillInfo | None:
        metadata = physical_data.get("metadata")
        if not isinstance(metadata, dict):
            self._logger.error(
                f"Physical skill '{skill_id}' has malformed metadata (type={type(metadata).__name__}); skipping"
            )
            return None

        directory = physical_data.get("directory", "") or ""
        try:
            episode_count = self.skill_loader._get_episode_count(directory)
        except Exception as e:
            self._logger.warn(f"Could not read episode count for '{skill_id}': {e}; defaulting to 0")
            episode_count = 0

        try:
            inputs_json = json.dumps(metadata.get("inputs", {}))
        except (TypeError, ValueError) as e:
            self._logger.error(f"Could not serialize inputs for physical skill '{skill_id}': {e}; using empty inputs")
            inputs_json = "{}"

        return self._build_skill_info(
            skill_id=skill_id,
            name=metadata.get("name", skill_id),
            skill_type=metadata.get("type", "physical"),
            guidelines=metadata.get("guidelines", ""),
            guidelines_when_running=metadata.get("guidelines_when_running", ""),
            inputs_json=inputs_json,
            in_training=in_training,
            episode_count=episode_count,
            directory=directory,
        )

    # --- cache ---
    def _skill_cache_path(self) -> Path:
        return Path(os.environ.get("INNATE_SKILL_CACHE", "/tmp/innate_skill_contracts.json"))

    def _skill_info_to_cache_dict(self, skill: SkillInfo) -> dict:
        try:
            inputs = json.loads(skill.inputs_json or "{}")
        except json.JSONDecodeError:
            inputs = {}
        if not isinstance(inputs, dict):
            inputs = {}
        return {
            "id": skill.id,
            "name": skill.name,
            "type": skill.type,
            "inputs": inputs,
            "guidelines": skill.guidelines,
            "guidelines_when_running": skill.guidelines_when_running,
            "in_training": skill.in_training,
            "episode_count": skill.episode_count,
            "directory": skill.directory,
        }

    def _write_skill_cache(self, skills: list[SkillInfo]) -> None:
        cache_path = self._skill_cache_path()
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "skills": [self._skill_info_to_cache_dict(skill) for skill in skills],
        }
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
            tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
            tmp_path.replace(cache_path)
        except OSError as exc:
            self._logger.warning(f"Failed to write skill cache {cache_path}: {exc}")
