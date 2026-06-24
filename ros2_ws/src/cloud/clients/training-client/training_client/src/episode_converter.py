"""
Convert HDF5 episodes from raw-image format to H.264 MP4 + stripped HDF5.

For each episode_N.h5 in the data directory:
  1. Move the original to raw_data/ (sibling of data/)
  2. Encode each camera's image stack to an MP4 via GStreamer x264enc
  3. Write a new episode_N.h5 without /observations/images/
  4. Update dataset_metadata.json (dataset_type h5 -> h264)

The conversion is incremental: episodes whose MP4s already exist on disk
are skipped.  New episodes added after a prior conversion are detected and
converted.  Partially-completed conversions are detected by the presence
of files in raw_data/ and resumed.

Datasets encoded by older Innate OS (before chroma was forced to 4:2:0) hold
H.264 4:4:4 MP4s that phone/Jetson hardware decoders render black.  Their raw
frames are long gone (the source H5 was stripped), so those MP4s are detected by
an encoder-version stamp and transcoded *in place from the MP4 itself* to a
universally decodable 4:2:0 stream.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Generator
from pathlib import Path

import h5py
import numpy as np

from .types import FileProgress, ProgressStage, ProgressUpdate

logger = logging.getLogger(__name__)

DATASET_METADATA = "dataset_metadata.json"
RAW_DATA_DIR = "raw_data"

# Bumped whenever the MP4 encode format changes. Each episode records the version
# it was encoded with (in dataset_metadata.json); any episode below this is
# re-encoded. v1 = H.264 4:2:0 chroma (forced via I420). Episodes from before this
# stamp existed — old Innate OS — are 4:4:4 and treated as v0, so they get
# transcoded to 4:2:0. Because the 4:2:0 fix and this stamp ship together, a
# below-version episode is *always* pre-fix 4:4:4, so no per-file probe is needed.
ENCODER_VERSION = 1


def convert_episodes_to_h264(
    data_dir: Path,
    *,
    nice_level: int = 0,
    threads: int = 4,
    idle_io: bool = False,
) -> Generator[ProgressUpdate, None, None]:
    """Convert all episodes under *data_dir* from raw-image HDF5 to H.264.

    Yields :class:`ProgressUpdate` with ``stage=COMPRESSING`` matching the
    existing upload progress pattern.

    ``nice_level``/``threads``/``idle_io`` tune the encoder's system priority.
    Defaults reproduce the original behavior (normal priority, 4 threads); the
    background ``dataset_encoder`` node passes a high nice level, fewer threads,
    and idle I/O so encoding never hiccups recording or live streaming.
    """
    meta_path = data_dir / DATASET_METADATA
    if not meta_path.is_file():
        logger.debug("No %s in %s — skipping H.264 conversion", DATASET_METADATA, data_dir)
        return

    meta = json.loads(meta_path.read_text())

    fps = int(meta.get("data_frequency", 30))
    episodes = meta.get("episodes", [])

    if not episodes:
        return

    raw_dir = data_dir.parent / RAW_DATA_DIR
    raw_dir.mkdir(exist_ok=True)

    work_items = _build_work_list(data_dir, raw_dir, episodes)
    if not work_items:
        logger.debug("No episodes need H.264 conversion")
        return

    total = len(work_items)

    for idx, item in enumerate(work_items, start=1):
        if item["kind"] == "mp4":
            h5_path = data_dir / item["h5_file"]
            raw_path = raw_dir / item["h5_file"]
            mp4_path = data_dir / item["filename"]
            cam_name = item["camera"]

            if mp4_path.exists():
                yield ProgressUpdate(
                    stage=ProgressStage.COMPRESSING,
                    message=f"[{idx}/{total}] {item['filename']} already exists, skipping",
                    file_progress=FileProgress(filename=item["filename"], index=idx, total=total, done=True),
                )
                continue

            yield ProgressUpdate(
                stage=ProgressStage.COMPRESSING,
                message=f"[{idx}/{total}] Encoding {item['filename']}…",
                file_progress=FileProgress(filename=item["filename"], index=idx, total=total),
            )

            try:
                with _convert_lock(data_dir):
                    if mp4_path.exists():
                        continue  # another converter finished it first
                    if not raw_path.exists():
                        if not h5_path.exists():
                            # deleted mid-pass — _update_metadata drops it
                            logger.info("Source for %s gone — skipping", item["filename"])
                            continue
                        shutil.move(str(h5_path), str(raw_path))
                    _encode_camera_to_mp4(
                        raw_path,
                        cam_name,
                        mp4_path,
                        fps,
                        nice_level=nice_level,
                        threads=threads,
                        idle_io=idle_io,
                    )
            except Exception as e:
                if not data_dir.is_dir():
                    # data/ removed mid-encode — the skill was converted to a replay
                    # skill (which drops its dataset) or deleted. The trajectory is
                    # preserved elsewhere, so this is benign; stop without erroring.
                    logger.info("Data dir %s gone mid-encode — skipping", data_dir)
                    return
                yield ProgressUpdate(
                    stage=ProgressStage.ERROR,
                    message=f"[{idx}/{total}] Encoding failed for {item['filename']}: {e}",
                    file_progress=FileProgress(
                        filename=item["filename"],
                        index=idx,
                        total=total,
                        error=str(e),
                    ),
                    error=str(e),
                )
                raise

        elif item["kind"] == "strip":
            h5_path = data_dir / item["filename"]
            raw_path = raw_dir / item["filename"]

            if not raw_path.exists():
                # Raw source deleted mid-pass — nothing left to strip.
                logger.info("Raw source for %s gone — skipping strip (deleted?)", item["filename"])
                continue

            yield ProgressUpdate(
                stage=ProgressStage.COMPRESSING,
                message=f"[{idx}/{total}] Stripping images from {item['filename']}…",
                file_progress=FileProgress(filename=item["filename"], index=idx, total=total),
            )

            try:
                with _convert_lock(data_dir):
                    if h5_path.exists():
                        continue  # another converter already stripped it
                    _strip_images_from_h5(raw_path, h5_path)
            except Exception as e:
                yield ProgressUpdate(
                    stage=ProgressStage.ERROR,
                    message=f"[{idx}/{total}] Stripping failed for {item['filename']}: {e}",
                    file_progress=FileProgress(
                        filename=item["filename"],
                        index=idx,
                        total=total,
                        error=str(e),
                    ),
                    error=str(e),
                )
                raise

        elif item["kind"] == "transcode":
            mp4_path = data_dir / item["filename"]
            if not mp4_path.exists():
                # MP4 deleted mid-pass (episode removed while we worked) — skip.
                logger.info("MP4 %s gone — skipping transcode (deleted?)", item["filename"])
                continue

            yield ProgressUpdate(
                stage=ProgressStage.COMPRESSING,
                message=f"[{idx}/{total}] Re-encoding {item['filename']} to 4:2:0…",
                file_progress=FileProgress(filename=item["filename"], index=idx, total=total),
            )

            try:
                with _convert_lock(data_dir):
                    _transcode_mp4_to_420(
                        mp4_path,
                        fps,
                        nice_level=nice_level,
                        threads=threads,
                        idle_io=idle_io,
                    )
            except Exception as e:
                yield ProgressUpdate(
                    stage=ProgressStage.ERROR,
                    message=f"[{idx}/{total}] Re-encode failed for {item['filename']}: {e}",
                    file_progress=FileProgress(
                        filename=item["filename"],
                        index=idx,
                        total=total,
                        error=str(e),
                    ),
                    error=str(e),
                )
                raise

    _update_metadata(data_dir)

    yield ProgressUpdate(
        stage=ProgressStage.COMPRESSING,
        message="H.264 conversion complete",
    )


def _build_work_list(data_dir: Path, raw_dir: Path, episodes: list[dict]) -> list[dict]:
    """Build a flat list of work items (MP4 encodes + H5 strips)."""
    items: list[dict] = []
    for ep in episodes:
        h5_file = ep.get("file_name", "")
        if not h5_file:
            continue

        # Re-encode stale MP4s: an episode encoded before the 4:2:0 chroma fix
        # (old Innate OS) has 4:4:4 MP4s that hardware decoders render black, and
        # its raw frames are gone (the source H5 was stripped), so we transcode
        # each existing MP4 in place from the MP4 itself. No pixel-format probe:
        # the chroma fix and the version stamp shipped together, so a below-version
        # episode is always pre-fix 4:4:4. _update_metadata stamps the episode
        # current once its MP4s are confirmed, so this runs exactly once.
        if ep.get("encoder_version", 0) < ENCODER_VERSION:
            for mp4 in sorted(data_dir.glob(f"{Path(h5_file).stem}_*.mp4")):
                items.append({"kind": "transcode", "filename": mp4.name})

        raw_path = raw_dir / h5_file
        h5_path = data_dir / h5_file
        source = raw_path if raw_path.exists() else h5_path

        if not source.exists():
            logger.warning("Episode file %s not found, skipping", h5_file)
            continue

        stem = h5_path.stem

        with h5py.File(str(source), "r") as f:
            obs = f.get("observations")
            if obs is None:
                continue
            images_grp = obs.get("images")
            if images_grp is None:
                continue

            all_mp4s_exist = True
            cam_items: list[dict] = []
            for cam_name in sorted(images_grp.keys()):
                mp4_name = f"{stem}_{cam_name}.mp4"
                if not (data_dir / mp4_name).exists():
                    all_mp4s_exist = False
                cam_items.append(
                    {
                        "kind": "mp4",
                        "filename": mp4_name,
                        "h5_file": h5_file,
                        "camera": cam_name,
                    }
                )

        if all_mp4s_exist and cam_items:
            if h5_path.exists():
                logger.debug("All MP4s exist for %s — skipping", h5_file)
                continue
            logger.info("All MP4s exist for %s but stripped H5 missing — queuing strip", h5_file)
            items.append({"kind": "strip", "filename": h5_file})
            continue

        items.extend(cam_items)
        items.append(
            {
                "kind": "strip",
                "filename": h5_file,
            }
        )

    return items


def _encode_camera_to_mp4(
    h5_path: Path,
    camera_name: str,
    mp4_path: Path,
    fps: int,
    *,
    nice_level: int = 0,
    threads: int = 4,
    idle_io: bool = False,
) -> None:
    """Read images for *camera_name* from *h5_path* and encode to MP4.

    Encodes to a ``.part`` sibling and atomically renames it into place on
    success, so a crash or kill mid-encode never leaves a half-written ``.mp4``
    that the incremental scan would trust as a finished file (and then strip the
    raw H5 / upload to the cloud).
    """
    part_path = mp4_path.with_name(mp4_path.name + ".part")
    part_path.unlink(missing_ok=True)  # clear any leftover from a prior crash
    with h5py.File(str(h5_path), "r") as f:
        dset = f[f"observations/images/{camera_name}"]
        num_frames, height, width, channels = dset.shape

        # Optional idle I/O class (best-effort; ionice may be absent on minimal
        # systems, in which case we just skip it rather than fail the encode).
        io_prefix = ["ionice", "-c", "3"] if (idle_io and shutil.which("ionice")) else []

        gst_args = [
            *io_prefix,
            "nice",
            "-n",
            str(nice_level),
            "gst-launch-1.0",
            "-e",
            "fdsrc",
            "fd=0",
            "!",
            "rawvideoparse",
            f"width={width}",
            f"height={height}",
            "format=bgr",
            f"framerate={fps}/1",
            "!",
            "videoconvert",
            "!",
            # Force 4:2:0 chroma. Without this, BGR input negotiates to H.264
            # 4:4:4 (profile High 4:4:4 Predictive), which hardware decoders —
            # phones and the Jetson's own nvv4l2decoder — cannot decode and
            # render black. I420 yields a universally decodable 4:2:0 stream.
            "video/x-raw,format=I420",
            "!",
            "x264enc",
            "pass=qual",
            "quantizer=23",
            "speed-preset=ultrafast",
            f"threads={threads}",
            # One keyframe per second. Default x264 keyint (250) leaves a short
            # episode as a single GOP, so seeking to any time decodes from frame
            # 0 — making scrubbing and the app's two-camera resync stall. A ~1s
            # GOP keeps seeks cheap.
            f"key-int-max={fps}",
            "!",
            "h264parse",
            "!",
            # faststart: moov atom at the front so the file streams/seeks over
            # HTTP Range without first fetching the tail.
            "mp4mux",
            "faststart=true",
            "!",
            "filesink",
            f"location={part_path}",
        ]

        proc = subprocess.Popen(
            gst_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        try:
            chunk_size = 64
            for start in range(0, num_frames, chunk_size):
                end = min(start + chunk_size, num_frames)
                frames = dset[start:end]
                if not np.isfortran(frames):
                    frames = np.ascontiguousarray(frames)
                proc.stdin.write(frames.tobytes())
        except BrokenPipeError:
            pass
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

        stderr_bytes = proc.stderr.read()
        proc.wait()

        if proc.returncode != 0:
            stderr = stderr_bytes.decode(errors="replace")
            part_path.unlink(missing_ok=True)
            raise RuntimeError(f"gst-launch-1.0 exited with code {proc.returncode}: {stderr}")

    # Encode succeeded and the file is fully muxed — publish it atomically so the
    # final .mp4 only ever appears complete.
    os.replace(str(part_path), str(mp4_path))

    raw_size = num_frames * height * width * channels
    mp4_size = mp4_path.stat().st_size
    logger.info(
        "Encoded %s/%s: %d frames, %.1f MB raw -> %.1f MB H.264 (%.1fx)",
        h5_path.name,
        camera_name,
        num_frames,
        raw_size / 1e6,
        mp4_size / 1e6,
        raw_size / max(mp4_size, 1),
    )


def _transcode_mp4_to_420(
    mp4_path: Path,
    fps: int,
    *,
    nice_level: int = 0,
    threads: int = 4,
    idle_io: bool = False,
) -> None:
    """Re-encode an existing MP4 in place to H.264 4:2:0 + faststart.

    For datasets from old Innate OS, the MP4s are 4:4:4 (encoded before chroma was
    forced to 4:2:0) and there are no raw frames left to re-encode from. So we
    decode the existing MP4 in *software* — ``avdec_h264`` handles 4:4:4, unlike
    the Jetson's hardware decoder — and re-encode to a universally decodable 4:2:0
    stream. It's a lossy→lossy re-encode, but the alternative is a file that
    phones and the Jetson render black. Writes a ``.part`` sibling and renames it
    atomically (like the raw-image encoder), so a crash/kill never leaves a
    half-written MP4 in place of the original.
    """
    part_path = mp4_path.with_name(mp4_path.name + ".part")
    part_path.unlink(missing_ok=True)  # clear any leftover from a prior crash

    io_prefix = ["ionice", "-c", "3"] if (idle_io and shutil.which("ionice")) else []
    gst_args = [
        *io_prefix,
        "nice",
        "-n",
        str(nice_level),
        "gst-launch-1.0",
        "-e",
        "filesrc",
        f"location={mp4_path}",
        "!",
        "qtdemux",
        "!",
        "h264parse",
        "!",
        # Software decode — handles the 4:4:4 stream the hardware decoder can't.
        "avdec_h264",
        "!",
        "videoconvert",
        "!",
        # Same 4:2:0 target + GOP/faststart settings as the raw-image encoder.
        "video/x-raw,format=I420",
        "!",
        "x264enc",
        "pass=qual",
        "quantizer=23",
        "speed-preset=ultrafast",
        f"threads={threads}",
        f"key-int-max={fps}",
        "!",
        "h264parse",
        "!",
        "mp4mux",
        "faststart=true",
        "!",
        "filesink",
        f"location={part_path}",
    ]

    proc = subprocess.run(gst_args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        part_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"gst-launch-1.0 transcode exited with code {proc.returncode}: {proc.stderr.decode(errors='replace')}"
        )
    if not part_path.exists() or part_path.stat().st_size == 0:
        # Never replace a good original with an empty/missing transcode.
        part_path.unlink(missing_ok=True)
        raise RuntimeError(f"transcode produced no output for {mp4_path.name}")

    os.replace(str(part_path), str(mp4_path))
    logger.info("Re-encoded %s to H.264 4:2:0", mp4_path.name)


def _strip_images_from_h5(raw_path: Path, dest_path: Path) -> None:
    """Create a copy of *raw_path* at *dest_path* without /observations/images/."""
    tmp_path = dest_path.with_suffix(".h5.tmp")
    try:
        with h5py.File(str(raw_path), "r") as src, h5py.File(str(tmp_path), "w") as dst:
            for key in src:
                if key == "observations":
                    obs_grp = dst.create_group("observations")
                    for obs_key in src["observations"]:
                        if obs_key != "images":
                            src["observations"].copy(obs_key, obs_grp)
                else:
                    src.copy(key, dst)

        os.replace(str(tmp_path), str(dest_path))
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    raw_size = raw_path.stat().st_size
    stripped_size = dest_path.stat().st_size
    logger.info(
        "Stripped %s: %.1f MB -> %.1f MB",
        dest_path.name,
        raw_size / 1e6,
        stripped_size / 1e6,
    )


@contextlib.contextmanager
def _metadata_lock(data_dir: Path):
    """Exclusive advisory lock around a dataset_metadata.json read-modify-write.

    The recorder (C++ ``TaskManager``) takes the same ``flock`` on the same
    ``.lock`` file for episode deletes / outcome edits, so those and this
    converter's metadata write can't interleave and clobber one another. Held
    only around the short RMW below — never across an encode.
    """
    lock_path = data_dir / (DATASET_METADATA + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the fd releases the flock


@contextlib.contextmanager
def _convert_lock(data_dir: Path):
    """Exclusive lock so the background encoder and a training upload can't
    convert the same item at once. Held only around one item's move/encode."""
    fd = os.open(str(data_dir / ".convert.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _update_metadata(data_dir: Path) -> None:
    """Flip ``dataset_type`` to h264 and attach per-episode ``video_files``.

    Re-reads dataset_metadata.json **fresh** under the shared lock rather than
    writing back the in-memory snapshot taken before the (minutes-long) encode.
    So a concurrent delete or outcome edit isn't lost: an episode removed during
    the encode stays removed (it won't be in the fresh read, so it can't be
    resurrected), and we only attach ``video_files`` to episodes that still
    exist and whose MP4s are on disk. Written atomically so a crash can't
    truncate it.
    """
    meta_path = data_dir / DATASET_METADATA
    with _metadata_lock(data_dir):
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            logger.warning("Cannot read %s for update — skipping", meta_path)
            return

        meta["dataset_type"] = "h264"
        for ep in meta.get("episodes", []):
            h5_file = ep.get("file_name", "")
            if not h5_file:
                continue
            stem = Path(h5_file).stem
            video_files = sorted(
                p.name for p in data_dir.iterdir() if p.name.startswith(stem + "_") and p.suffix == ".mp4"
            )
            if video_files:
                ep["video_files"] = video_files
                # Stamp the format these MP4s are now in, so the next scan doesn't
                # re-probe or re-transcode them (this runs after any transcode, so
                # the files are guaranteed current here).
                ep["encoder_version"] = ENCODER_VERSION

        tmp_path = meta_path.with_suffix(meta_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(meta, indent=4) + "\n")
        os.replace(str(tmp_path), str(meta_path))
    logger.info("Updated %s: dataset_type -> h264", DATASET_METADATA)
