"""Background workers: polling, upload, download.

All functions in this module run on daemon threads and communicate with
the rest of the node exclusively through :class:`JobStore` and the
``rclpy`` logger.

Downloads automatically activate the run (set checkpoint + stats in
``metadata.json``) once the transfer completes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import rclpy.logging
from innate_cloud_msgs.msg import TransferProgress
from training_client.src.skill_manager import (
    SkillManager,
    read_local_episode_count,
    write_uploaded_episode_count,
)
from training_client.src.types import ProgressStage, ProgressUpdate

from .job_store import JobStore

_ros = rclpy.logging.get_logger("innate_training")

# Mirror of manipulation.config_validation.DEFAULT_ACTION_DIM (the engine pre-build's
# producer-side default). Duplicated as a literal because this long-running node
# deliberately never imports the manipulation package -- only the short-lived engine-builder
# subprocess gets it on PYTHONPATH (see _engine_builder_env). Keep in sync with that constant
# so a skill whose metadata omits execution.action_dim builds and loads the same engine.
_DEFAULT_ACTION_DIM = 10


class Poller:
    """Periodically fetches all jobs, polls active runs, and starts uploads."""

    def __init__(
        self,
        manager: SkillManager,
        store: JobStore,
        poll_interval: float,
    ) -> None:
        self._mgr = manager
        self._store = store
        self._interval = poll_interval
        self._shutting_down = False

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="training_poll").start()

    def stop(self) -> None:
        self._shutting_down = True

    # ── Main loop ───────────────────────────────────────────────────

    def _loop(self) -> None:
        try:
            self._fetch_all()
        except Exception as e:
            _ros.error(f"Initial fetch failed: {e}")

        while not self._shutting_down:
            try:
                self._poll_active()
            except Exception as e:
                _ros.warning(f"Poll cycle error: {e}")
            time.sleep(self._interval)

    def _fetch_all(self) -> None:
        _ros.info("Fetching all skills and runs from server…")
        try:
            skills = self._mgr.list_skills()
        except Exception as e:
            _ros.error(f"list_skills failed: {e}")
            return

        for skill in skills:
            self._store.put_skill(skill)
        _ros.info(f"Fetched {len(skills)} skill(s)")

        total_runs = 0
        done_runs = 0
        for skill in skills:
            try:
                runs = self._mgr.list_runs(skill.skill_id)
                for run in runs:
                    total_runs += 1
                    self._store.put_job(run)
                    _ros.debug(
                        f"  skill={skill.name or skill.skill_id[:8]} "
                        f"run={run.run_id} status={run.status}"
                        f"{' daemon=' + run.daemon_state if run.daemon_state else ''}"
                    )
                    if run.status == "done":
                        done_runs += 1
                        dest = self._store.dir_for_skill(run.skill_id)
                        if dest:
                            maybe_auto_download(
                                self._mgr,
                                self._store,
                                run.skill_id,
                                run.run_id,
                                dest,
                            )
            except Exception as e:
                _ros.warning(f"list_runs({skill.skill_id[:8]}) failed: {e}")

        _ros.info(f"Startup fetch complete: {len(skills)} skill(s), {total_runs} run(s) ({done_runs} done)")

    def _poll_active(self) -> None:
        for (skill_id, run_id), old in self._store.active_jobs():
            if self._shutting_down:
                return
            sid = skill_id[:8]
            try:
                new = self._mgr.run_status(skill_id, run_id)
                self._store.put_job(new)

                status_changed = new.status != old.status
                daemon_changed = new.daemon_state != old.daemon_state

                if status_changed or daemon_changed:
                    parts = [f"Run {sid}/{run_id}: {old.status}→{new.status}"]
                    if new.daemon_state:
                        parts.append(f"daemon={new.daemon_state}")
                    if new.instance_type and new.status in ("booting", "running"):
                        parts.append(f"instance={new.instance_type}")
                    if new.instance_ip and new.status == "running":
                        parts.append(f"ip={new.instance_ip}")
                    _ros.info(" | ".join(parts))

                    if new.error_message:
                        _ros.warning(f"Run {sid}/{run_id} error: {new.error_message}")

                if new.status == "done" and old.status != "done":
                    _ros.info(f"Run {sid}/{run_id} done → auto-download")
                    dest = self._store.dir_for_skill(skill_id)
                    if dest:
                        maybe_auto_download(
                            self._mgr,
                            self._store,
                            skill_id,
                            run_id,
                            dest,
                        )
                    else:
                        _ros.warning(f"No skill dir known for {sid} — skipping auto-download")
            except Exception as e:
                _ros.warning(f"Poll {sid}/{run_id} failed: {e}")


# ── Upload worker ───────────────────────────────────────────────────


def do_upload(
    manager: SkillManager,
    store: JobStore,
    skill_id: str,
    skill_dir: str,
) -> bool:
    """Upload data files for *skill_id*. Returns True on success, False if the
    upload failed (lets callers gate a follow-up action like creating a run)."""
    sid = skill_id[:8]
    prev_stage: str | None = None
    prev_msg: str | None = None
    try:
        for update in manager.upload_files(skill_id, skill_dir):
            store.update_transfer(TransferProgress.UPLOAD, skill_id, -1, update)
            stage = update.stage.value
            if stage != prev_stage or update.message != prev_msg:
                _ros.info(f"Upload {sid}: {stage} — {update.message}")
                prev_stage = stage
                prev_msg = update.message
        _ros.info(f"Upload finished for {sid} from {skill_dir}")
    except Exception as e:
        _ros.error(f"Upload failed for {sid}: {e}")
        store.update_transfer(
            TransferProgress.UPLOAD,
            skill_id,
            -1,
            ProgressUpdate(
                stage=ProgressStage.ERROR,
                message=f"Upload failed: {e}",
                skill_id=skill_id,
                error=str(e),
            ),
        )
        return False

    # Persist the episode count at the time of this successful upload.
    try:
        ep_count = read_local_episode_count(skill_dir)
        write_uploaded_episode_count(skill_dir, ep_count)
        store.set_uploaded_ep_count(skill_id, ep_count)
        _ros.info(f"Persisted uploaded_episode_count={ep_count} for {sid}")
    except Exception as e:
        _ros.warning(f"Failed to persist uploaded_episode_count for {sid}: {e}")

    store.update_transfer(
        TransferProgress.UPLOAD,
        skill_id,
        -1,
        ProgressUpdate(
            stage=ProgressStage.DONE,
            message="Upload complete",
            skill_id=skill_id,
        ),
    )
    return True


# ── Download worker ─────────────────────────────────────────────────


def _engine_builder_env() -> dict:
    """Environment for the act_trt subprocess.

    ``manipulation`` is an ament_cmake package whose Python lives in
    ``<prefix>/lib/manipulation/`` and is NOT on PYTHONPATH by default -- the ROS node
    only imports it because it runs as a script from that directory. A subprocess spawned
    from this (different) package doesn't inherit that, so ``python -m manipulation.act_trt``
    would fail with ModuleNotFoundError. Put the package's lib dir on PYTHONPATH explicitly.
    """
    env = os.environ.copy()
    try:
        from ament_index_python.packages import get_package_prefix

        manip_lib = os.path.join(get_package_prefix("manipulation"), "lib", "manipulation")
    except Exception as e:  # noqa: BLE001 - best-effort; subprocess will narrate if it can't import
        _ros.warning(f"Could not locate the manipulation package for the engine builder: {e}")
        return env
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = manip_lib + (os.pathsep + existing if existing else "")
    return env


def _action_dim_for(skill_dir: str) -> int:
    """Read ``execution.action_dim`` from a skill's metadata.json, defaulting to _DEFAULT_ACTION_DIM."""
    meta_path = os.path.join(skill_dir, "metadata.json")
    try:
        with open(meta_path) as f:
            return int(json.load(f).get("execution", {}).get("action_dim", _DEFAULT_ACTION_DIM))
    except Exception:
        return _DEFAULT_ACTION_DIM  # fall back to the default action_dim


def _run_engine_builder(skill_dir: str, checkpoint_rel: str) -> None:
    """Build & cache the TensorRT engine for a checkpoint via a short-lived subprocess.

    Runs the manipulation package's builder out-of-process so this long-running node
    never imports torch/tensorrt itself. The engine is cached next to the checkpoint;
    manipulation_server loads it at runtime. Raises on failure -- callers narrate. The
    builder is a fast no-op when the current-version engine already exists.
    """
    checkpoint_path = os.path.join(skill_dir, checkpoint_rel)
    if not os.path.isfile(checkpoint_path):
        raise FileNotFoundError(checkpoint_path)

    subprocess.run(
        [sys.executable, "-m", "manipulation.act_trt", checkpoint_path, str(_action_dim_for(skill_dir)), "fp32"],
        check=True,
        timeout=600,
        env=_engine_builder_env(),
    )


def _prebuild_trt_engine(skill_dir: str, checkpoint_rel: str) -> None:
    """Pre-build the TensorRT engine for a freshly activated checkpoint (download path).

    Best-effort -- if the build can't run (no tensorrt, etc.) the engine is simply
    built lazily on first use.
    """
    _ros.info(f"Pre-building TensorRT engine for {os.path.basename(checkpoint_rel)} (one-time, ~1 min) ...")
    _run_engine_builder(skill_dir, checkpoint_rel)
    _ros.info("TensorRT engine ready")


def do_download(
    manager: SkillManager,
    store: JobStore,
    skill_id: str,
    run_id: int,
    dest_dir: str,
) -> None:
    """Download & decompress result files for a run."""
    sid = skill_id[:8]
    prev_stage: str | None = None
    t0 = time.monotonic()
    try:
        for update in manager.download(skill_id, run_id, dest_dir=dest_dir):
            store.update_transfer(TransferProgress.DOWNLOAD, skill_id, run_id, update)
            stage = update.stage.value
            if stage != prev_stage:
                _ros.info(f"Download {sid}/{run_id}: {stage} — {update.message}")
                prev_stage = stage
            fp = update.file_progress
            if fp and fp.bytes_done == 0 and not fp.done:
                _ros.debug(
                    f"Download {sid}/{run_id}: file [{fp.index}/{fp.total}] {fp.filename} ({fp.bytes_total} bytes)"
                )
        # Clear finished transfer entry.
        store.update_transfer(
            TransferProgress.DOWNLOAD,
            skill_id,
            run_id,
            ProgressUpdate(
                stage=ProgressStage.DONE,
                message="Download complete",
                skill_id=skill_id,
                run_id=run_id,
            ),
        )
        # Refresh cached state (download marks the run as "downloaded").
        try:
            store.put_job(manager.run_status(skill_id, run_id))
        except Exception:
            pass

        # Activate: set checkpoint + stats_file in metadata.json.
        result = None
        try:
            result = manager.activate_run(dest_dir, run_id)
            _ros.info(
                f"Activated run {sid}/{run_id}: checkpoint={result['checkpoint']} stats_file={result['stats_file']}"
            )
        except Exception as e:
            # Activation failed: files are on disk but execution.checkpoint was
            # never written, so the skill is un-runnable and the startup sweep
            # can't recover it. Clear the download mark so a later poll retries.
            _ros.error(f"Download OK but activation failed for {sid}/{run_id}: {e}")
            store.unmark_download(skill_id, run_id)

        # Pre-build the TensorRT inference engine so the first execution is fast
        # (the engine is otherwise built lazily on first run, costing ~1 min).
        # Best-effort: failures here never affect the download/activation result.
        if result is not None:
            try:
                _prebuild_trt_engine(dest_dir, result["checkpoint"])
            except Exception as e:
                _ros.warning(f"TensorRT engine pre-build skipped for {sid}/{run_id}: {e}")

        elapsed = time.monotonic() - t0
        _ros.info(f"Download finished for {sid}/{run_id} → {dest_dir} ({elapsed:.1f}s)")
    except Exception as e:
        _ros.error(f"Download failed for {sid}/{run_id}: {e}")
        store.update_transfer(
            TransferProgress.DOWNLOAD,
            skill_id,
            run_id,
            ProgressUpdate(
                stage=ProgressStage.ERROR,
                message=f"Download failed: {e}",
                skill_id=skill_id,
                run_id=run_id,
                error=str(e),
            ),
        )


# ── Auto-download helper ───────────────────────────────────────────


def maybe_auto_download(
    manager: SkillManager,
    store: JobStore,
    skill_id: str,
    run_id: int,
    download_dir: str,
) -> None:
    """Spawn a download thread if one hasn't been started already."""
    if not store.mark_download_started(skill_id, run_id):
        return

    threading.Thread(
        target=do_download,
        args=(manager, store, skill_id, run_id, download_dir),
        daemon=True,
        name=f"dl-{skill_id[:8]}-{run_id}",
    ).start()


# ── Startup engine pre-build sweep ──────────────────────────────────


def _skill_scan_roots() -> list[str]:
    """All skill roots the loader recognizes, via brain_client's canonical getter.

    Defers to ``get_skill_directories`` -- the same source of truth the loader uses --
    so the sweep can never drift from it. ``brain_client`` is an exec_depend of this
    package, so the import is expected to resolve; the caller treats a failure here as a
    best-effort skip (engines then build lazily on first use) rather than re-deriving the
    root list and risking divergence.
    """
    from brain_client.common.script_paths import get_skill_directories

    return [str(d) for d in get_skill_directories()]


def prebuild_all_engines() -> None:
    """Ensure every local skill checkpoint has a current-version TensorRT engine.

    Complements the on-download pre-build, which only fires for a fresh download.
    This covers checkpoints already on disk (downloaded before pre-build existed,
    shipped skills) and engines invalidated by a TensorRT upgrade (the engine cache
    key is version-scoped). Best-effort and idempotent -- the builder is a fast
    no-op when the engine already exists.
    """
    try:
        scan_roots = _skill_scan_roots()
    except Exception as e:  # noqa: BLE001 - best-effort; engines build lazily on first use
        _ros.warning(f"TensorRT engine sweep skipped (could not resolve skill roots: {e})")
        return

    targets: list[dict] = []
    for scan_root in scan_roots:
        if not os.path.isdir(scan_root):
            continue
        for entry in sorted(os.listdir(scan_root)):
            skill_dir = os.path.join(scan_root, entry)
            meta_path = os.path.join(skill_dir, "metadata.json")
            if not os.path.isfile(meta_path):
                continue
            try:
                with open(meta_path) as f:
                    checkpoint_rel = json.load(f).get("execution", {}).get("checkpoint")
            except Exception:
                checkpoint_rel = None
            if not checkpoint_rel:
                continue
            checkpoint_path = os.path.join(skill_dir, checkpoint_rel)
            if os.path.isfile(checkpoint_path):
                targets.append({"checkpoint": checkpoint_path, "action_dim": _action_dim_for(skill_dir)})

    if not targets:
        return

    # One subprocess builds them all: the act_trt builder pays the torch/tensorrt import
    # (and CUDA init) once for the whole sweep instead of a cold process per checkpoint.
    # Each target is a fast no-op when its engine is already current, so a steady-state
    # boot just imports once and exits. Best-effort: the builder logs per-checkpoint and
    # never fails the batch, so any genuine build problem just falls back to a lazy build
    # on first run.
    _ros.info(f"Ensuring TensorRT engines for {len(targets)} skill checkpoint(s) …")
    try:
        subprocess.run(
            [sys.executable, "-m", "manipulation.act_trt", "--batch"],
            input=json.dumps(targets),
            text=True,
            check=True,
            timeout=max(600, 120 * len(targets)),
            env=_engine_builder_env(),
        )
    except Exception as e:  # noqa: BLE001 - best-effort; engines otherwise build lazily on first use
        _ros.warning(f"TensorRT engine sweep skipped: {e}")
    _ros.info("TensorRT engine sweep complete")


def start_prebuild_sweep() -> None:
    """Run :func:`prebuild_all_engines` on a daemon thread (non-blocking, best-effort)."""
    threading.Thread(target=prebuild_all_engines, daemon=True, name="trt-prebuild-sweep").start()
