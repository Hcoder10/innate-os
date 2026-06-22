"""TensorRT acceleration for ACT policy inference (the only inference path).

On Jetson Orin the eager ACT forward is ~64 ms; a fused TensorRT engine runs the
same forward in ~6 ms fp32 (~10x, ~0.02% RMSE) or ~3 ms fp16 (~20x, ~0.08% RMSE),
using roughly half the RAM. The default precision is fp32 for near-exact accuracy.
This module exports the ACT inference graph to ONNX, builds a cached engine per
checkpoint, and provides a ``select_action``/``reset`` runner (the engine does
normalize->model->unnormalize; the action queue, chunk slicing and speed resampling
stay in Python). An eager ``ACTPolicy`` is still constructed at load time, but only
to export the engine -- it is released as soon as the engine is built.

Used by manipulation_server and pre-built in the model-download flow
(innate_training_node). Requires the ``tensorrt`` Python package (JetPack) and, for
the one-time build only, ``onnx``. Importing this module fails if ``tensorrt`` is
missing -- that is intentional: TensorRT is the only inference path, not an option.

Note: on this JetPack the ``tensorrt`` Python import needs ``libnvdla_compiler.so``
on the loader path. If it's missing, install ``nvidia-l4t-dla-compiler`` (or put the
extracted lib on ``LD_LIBRARY_PATH``).
"""

import hashlib
import os
from collections import deque

import tensorrt as trt
import torch
import torch.nn as nn
import torch.nn.functional as F


class _ExportWrapper(nn.Module):
    """Flatten ACTPolicy inference to plain tensors for ONNX export.

    Input: raw images + state. Output: full unnormalized action chunk
    (B, chunk_size, action_dim) -- i.e. normalize -> model -> unnormalize.
    """

    def __init__(self, policy):
        super().__init__()
        self.policy = policy

    @torch.no_grad()
    def forward(self, image_camera_1, image_camera_2, state):
        batch = {
            "observation.image_camera_1": image_camera_1,
            "observation.image_camera_2": image_camera_2,
            "observation.state": state,
        }
        normalized = self.policy.normalize_inputs(batch)
        model_batch = self.policy._prepare_batch_for_model(normalized)
        chunk_normalized = self.policy.model(model_batch)[0]
        return self.policy.unnormalize_outputs({"action": chunk_normalized})["action"]


def _dataset_stats_signature(checkpoint_path: str) -> str:
    """Short content hash of the sibling dataset_stats.pt.

    The normalize->model->unnormalize graph constant-folds the dataset stats (mean/std)
    into the engine, but the engine cache is otherwise keyed only by checkpoint path. If
    the stats are regenerated in place while the checkpoint path stays the same, keying on
    the stats content forces a rebuild instead of silently reusing an engine that bakes the
    old normalization (which would produce systematically wrong actions). ``nostats`` when
    absent.
    """
    stats_path = os.path.join(os.path.dirname(checkpoint_path), "dataset_stats.pt")
    try:
        with open(stats_path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()[:8]
    except OSError:
        return "nostats"


def engine_path_for(checkpoint_path: str, action_dim: int, chunk_size: int, precision: str = "fp32") -> str:
    """Deterministic, version-scoped engine cache path next to the checkpoint.

    The TensorRT version, precision, the architecture dims baked into the engine
    (action_dim, chunk_size), and a hash of the normalization stats are all in the
    filename so a TRT upgrade, a precision change, a checkpoint resolved with different
    dims, or regenerated stats transparently builds a fresh engine instead of silently
    reusing one whose I/O shapes or baked constants no longer match. The checkpoint path
    is normalized (abspath) so the pre-build and the runtime load agree on the cache name
    regardless of how each spelled the path (trailing slash, relative segments, ~).
    """
    checkpoint_path = os.path.abspath(os.path.expanduser(checkpoint_path))
    version = trt.__version__
    stats_sig = _dataset_stats_signature(checkpoint_path)
    return f"{checkpoint_path}.{precision}.trt{version}.a{action_dim}.c{chunk_size}.s{stats_sig}.bs1.engine"


def _timing_cache_path() -> str:
    """Shared kernel-timing cache path (per TRT version / GPU). All ACT policies share
    one architecture, so this cache makes every build after the first ~7x faster."""
    version = trt.__version__
    return os.path.expanduser(f"~/.cache/innate_trt/act.trt{version}.timing.cache")


def build_engine(policy, checkpoint_path: str, device, precision: str = "fp32", log=print) -> str:
    """Export the ACT inference graph to ONNX and build a cached TRT engine.

    Returns the engine path. The build (ONNX export + engine optimization) runs once
    per checkpoint; later calls reuse the cached engine. A shared kernel-timing cache
    makes the build ~44s the first time, then ~6s for every subsequent checkpoint
    (same architecture). Fixed batch size 1 / fixed input shapes.
    """
    action_dim = policy.config.output_shapes["action"][0]
    chunk_size = policy.config.chunk_size
    engine_path = engine_path_for(checkpoint_path, action_dim, chunk_size, precision)
    if os.path.exists(engine_path):
        return engine_path

    import onnx  # noqa: F401 - build-time only, validates the exported graph

    image_shape = policy.config.input_shapes["observation.image_camera_1"]
    state_dim = policy.config.input_shapes["observation.state"][0]
    # Per-process scratch paths: the startup sweep and the on-download pre-build can both build
    # the same checkpoint at once (the os.path.exists(engine_path) fast-path doesn't cover the
    # first-ever build). Sharing engine_path+".onnx" lets one build's torch.onnx.export truncate
    # the file mid parser.parse() in the other, failing the parse. The final engine write is
    # already atomic (os.replace), and both builds produce an identical engine, so per-PID
    # scratch + last-writer-win is race-free.
    scratch_prefix = f"{engine_path}.{os.getpid()}"
    onnx_path = scratch_prefix + ".onnx"
    dummy = (
        torch.zeros(1, *image_shape, device=device),
        torch.zeros(1, *image_shape, device=device),
        torch.zeros(1, state_dim, device=device),
    )

    tmp_path = scratch_prefix + ".tmp"
    # The ONNX export and the .tmp engine are scratch files; clean them up no matter how we
    # exit. Without the finally, a failed parse/build (e.g. CUDA OOM) leaks a multi-hundred-MB
    # .onnx next to the checkpoint, and the startup sweep re-creates it on every boot.
    try:
        log(f"[trt] exporting ONNX for {os.path.basename(checkpoint_path)} ...")
        wrapper = _ExportWrapper(policy).eval().to(device)
        torch.onnx.export(
            wrapper,
            dummy,
            onnx_path,
            input_names=["image_camera_1", "image_camera_2", "state"],
            output_names=["action_chunk"],
            opset_version=17,
            do_constant_folding=True,
            dynamo=False,
        )
        onnx.checker.check_model(onnx.load(onnx_path))

        log(f"[trt] building {precision} engine (one-time, can take 1-2 min) ...")
        logger = trt.Logger(trt.Logger.WARNING)
        builder = trt.Builder(logger)
        network = builder.create_network(1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH))
        parser = trt.OnnxParser(network, logger)
        with open(onnx_path, "rb") as f:
            if not parser.parse(f.read()):
                errors = "; ".join(str(parser.get_error(i)) for i in range(parser.num_errors))
                raise RuntimeError(f"ONNX parse failed: {errors}")

        config = builder.create_builder_config()
        if precision == "fp16":
            config.set_flag(trt.BuilderFlag.FP16)

        # Shared timing cache: kernel autotuning (the bulk of build time) is reused across
        # ACT checkpoints since they share one architecture (~44s first build -> ~6s after).
        # The cache is GPU/TRT-version specific; failures here just fall back to a full build.
        timing_cache = None
        cache_path = _timing_cache_path()
        try:
            if os.path.exists(cache_path):
                with open(cache_path, "rb") as f:
                    prev = f.read()
            else:
                prev = b""
            timing_cache = config.create_timing_cache(prev)
            config.set_timing_cache(timing_cache, ignore_mismatch=False)
        except Exception as e:  # noqa: BLE001
            log(f"[trt] timing cache unavailable ({e}); building without it")

        serialized = builder.build_serialized_network(network, config)
        if serialized is None:
            raise RuntimeError("TensorRT engine build returned None")
        # Write atomically: a process killed mid-write (OOM, disk full, SIGKILL) must not
        # leave a truncated engine at engine_path, which would poison the cache (the next
        # load sees the file, skips the rebuild, and fails to deserialize forever).
        with open(tmp_path, "wb") as f:
            f.write(serialized)
        os.replace(tmp_path, engine_path)

        if timing_cache is not None:
            try:
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                with open(cache_path, "wb") as f:
                    f.write(timing_cache.serialize())
            except OSError:
                pass
    finally:
        for scratch in (onnx_path, tmp_path):
            try:
                os.remove(scratch)
            except OSError:
                pass

    log(f"[trt] engine cached at {engine_path}")
    return engine_path


class TRTACTPolicy:
    """Drop-in replacement for ACTPolicy's inference interface backed by a TRT engine.

    The engine computes the full unnormalized action chunk; chunk consumption stays in
    Python and mirrors ACTPolicy exactly. Two modes:

    - ``temporal_ensemble_coeff`` set: run the engine every step and exponentially blend
      overlapping chunk predictions (ACTPolicy's temporal-ensemble path). This removes the
      chunk-boundary discontinuity you'd otherwise see ~once per replan. Only feasible
      because the TRT forward (~6ms) fits the 25Hz loop -- eager (~64ms) can't run per-step.
    - otherwise: the chunked action queue (slice n_action_steps, resample, popleft).

    Holds no torch weights, so the eager model can be released after the engine is built.
    """

    def __init__(self, engine_path: str, config, device, temporal_ensemble_coeff: float | None = None):
        self.config = config
        self.device = device
        action_dim = config.output_shapes["action"][0]
        image_shape = config.input_shapes["observation.image_camera_1"]
        state_dim = config.input_shapes["observation.state"][0]

        self._runtime = trt.Runtime(trt.Logger(trt.Logger.WARNING))
        with open(engine_path, "rb") as f:
            self._engine = self._runtime.deserialize_cuda_engine(f.read())
        if self._engine is None:
            raise RuntimeError(
                f"Failed to deserialize TRT engine at {engine_path} "
                "(incompatible/corrupt cache -- delete it to rebuild)"
            )
        self._context = self._engine.create_execution_context()
        self._stream = torch.cuda.Stream()

        # Persistent I/O buffers (fixed batch size 1); engine addresses bound once.
        self._buffers = {
            "image_camera_1": torch.empty(1, *image_shape, device=device),
            "image_camera_2": torch.empty(1, *image_shape, device=device),
            "state": torch.empty(1, state_dim, device=device),
            "action_chunk": torch.empty(1, config.chunk_size, action_dim, device=device),
        }
        # Validate the deserialized engine's I/O against our buffers before binding raw
        # pointers: set_tensor_address does no size checking, so a cached engine built for a
        # different architecture (e.g. a stale file from older code) would otherwise read/write
        # out of bounds. A mismatch raises here so the policy load fails loudly instead.
        for i in range(self._engine.num_io_tensors):
            name = self._engine.get_tensor_name(i)
            if name not in self._buffers:
                raise RuntimeError(f"Engine I/O tensor {name!r} has no matching buffer (engine/arch mismatch)")
            expected = tuple(self._buffers[name].shape)
            actual = tuple(self._engine.get_tensor_shape(name))
            if actual != expected:
                raise RuntimeError(
                    f"Engine tensor {name!r} shape {actual} != expected {expected}; "
                    "cached engine was built for a different architecture"
                )
            self._context.set_tensor_address(name, self._buffers[name].data_ptr())

        self._ensembler = None
        if temporal_ensemble_coeff is not None:
            from manipulation.ACT import ACTTemporalEnsembler  # noqa: PLC0415

            # Match ACTPolicy: ensemble over the resampled chunk length.
            effective_chunk_size = int(config.chunk_size / config.speed)
            self._ensembler = ACTTemporalEnsembler(temporal_ensemble_coeff, effective_chunk_size)

        self.reset()

    def reset(self):
        if self._ensembler is not None:
            self._ensembler.reset()
        else:
            self._action_queue = deque([], maxlen=self.config.n_action_steps)

    def _resample_actions(self, actions, speed):
        # Linearly resample the action chunk along the time axis to apply the speed factor:
        # speed > 1 shortens the chunk (faster playback), speed < 1 lengthens it.
        batch_size, seq_len, action_dim = actions.shape
        new_seq_len = int(seq_len / speed)
        if new_seq_len == 0:
            raise ValueError(f"Speed factor {speed} results in zero sequence length for input length {seq_len}")
        if new_seq_len == seq_len:
            return actions
        actions_transposed = actions.transpose(1, 2)
        resampled = F.interpolate(actions_transposed, size=new_seq_len, mode="linear", align_corners=True)
        return resampled.transpose(1, 2)

    def _run_engine(self, batch):
        """Run the engine for the current observation; returns the full unnormalized
        action chunk (1, chunk_size, action_dim), cloned out of the persistent buffer."""
        # Run the input copies AND the engine launch on self._stream, after waiting for the
        # caller's stream (where the input tensors were produced). Without this, the copies
        # would run on the default stream while the engine ran on self._stream with no
        # ordering between them -- the engine could read the input buffers before the copies
        # land, yielding actions computed from stale/partial data.
        self._stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(self._stream):
            self._buffers["image_camera_1"].copy_(batch["observation.image_camera_1"])
            self._buffers["image_camera_2"].copy_(batch["observation.image_camera_2"])
            self._buffers["state"].copy_(batch["observation.state"])
            ok = self._context.execute_async_v3(self._stream.cuda_stream)
        self._stream.synchronize()
        if not ok:
            # A failed launch (CUDA OOM, driver fault, unbound tensor) leaves stale data in
            # action_chunk; raise so the caller skips this step instead of acting on it.
            raise RuntimeError("TensorRT execute_async_v3 failed (engine execution did not launch)")
        return self._buffers["action_chunk"].clone()

    @torch.no_grad()
    def select_action(self, batch):
        if self._ensembler is not None:
            # Run the model every step and blend overlapping predictions (no chunk boundary).
            chunk = self._run_engine(batch)
            if self.config.speed != 1:
                chunk = self._resample_actions(chunk, self.config.speed)
            return self._ensembler.update(chunk)

        # Chunked path: run the model only when the queue drains.
        if len(self._action_queue) == 0:
            chunk = self._run_engine(batch)
            actions_to_queue = chunk[:, : self.config.n_action_steps]
            if self.config.speed != 1:
                actions_to_queue = self._resample_actions(actions_to_queue, self.config.speed)
            self._action_queue.extend(actions_to_queue.transpose(0, 1))
        return self._action_queue.popleft()

    def __del__(self):
        # TensorRT requires teardown in order: execution context -> engine -> runtime.
        # Python doesn't guarantee attribute GC order, so drop them explicitly. This runs on
        # every policy reload (_release_policy does del + gc.collect), not just at shutdown.
        for attr in ("_context", "_engine", "_runtime"):
            try:
                delattr(self, attr)
            except Exception:  # noqa: BLE001 - best-effort cleanup; attrs may be absent if __init__ failed
                pass


def _main():
    """Offline engine pre-build.

    Single:  python -m manipulation.act_trt <checkpoint.pth> [action_dim=10] [fp32|fp16]
    Batch:   python -m manipulation.act_trt --batch
             Reads a JSON array of {"checkpoint", "action_dim", "precision"} objects from
             stdin and builds each. The startup sweep uses this so the torch/tensorrt import
             and CUDA init are paid once for the whole sweep instead of spawning a cold
             process per checkpoint -- a steady-state boot (every engine already current)
             then just imports once and hits the per-checkpoint fast path.
    """
    import sys

    # Run via `python -m manipulation.act_trt`, so the `manipulation` package is on the
    # path and we import it the same way the ROS node does -- a single ACT module, no
    # sys.path hacking and no duplicate ACTConfig/ACTPolicy class objects.
    from manipulation.ACT import ACTPolicy  # noqa: PLC0415 - standalone entrypoint
    from manipulation.act_config import (  # noqa: PLC0415 - ROS-free arch factory
        DEFAULT_ACTION_DIM,
        create_act_config,
        infer_chunk_size,
        load_torch_file,
        normalize_state_dict,
        validate_action_dim,
    )

    device = torch.device("cuda")

    def build_one(checkpoint_path, action_dim, precision):
        checkpoint_dir = os.path.dirname(checkpoint_path)

        # The engine path depends on action_dim (CLI arg) and chunk_size, and chunk_size is
        # only knowable from the checkpoint. The state_dict load below is mmap'd, so reading
        # one tensor's shape is cheap -- do it first so the fast path can skip the expensive
        # work (dataset_stats load, model construction, device transfer, ONNX export + build).
        # Normalize first so wrapped/compiled checkpoints resolve like they do in the server.
        raw_state_dict = load_torch_file(checkpoint_path, mmap=True, log=print)
        state_dict = normalize_state_dict(raw_state_dict)
        chunk_size = infer_chunk_size(state_dict)

        # Fast path: if the current-version engine already exists, skip building the model.
        # Keeps the startup sweep cheap when engines exist.
        existing = engine_path_for(checkpoint_path, action_dim, chunk_size, precision)
        if os.path.exists(existing):
            print(f"Engine already current: {existing}")
            return

        # Fail loudly on a metadata/checkpoint action_dim mismatch instead of caching an
        # engine with a wrong-width action head (see act_config.validate_action_dim).
        validate_action_dim(state_dict, action_dim, checkpoint_path)

        dataset_stats = load_torch_file(os.path.join(checkpoint_dir, "dataset_stats.pt"), log=print)
        config = create_act_config(action_dim=action_dim, chunk_size=chunk_size)
        policy = ACTPolicy(config=config, dataset_stats=dataset_stats)
        policy.load_state_dict(state_dict, strict=False, assign=True)
        policy = policy.to(device).eval()
        path = build_engine(policy, checkpoint_path, device, precision=precision)
        print(f"Built engine: {path}")

    if len(sys.argv) > 1 and sys.argv[1] == "--batch":
        import json  # noqa: PLC0415

        for target in json.load(sys.stdin):
            checkpoint_path = target["checkpoint"]
            try:
                build_one(
                    checkpoint_path,
                    int(target.get("action_dim", DEFAULT_ACTION_DIM)),
                    target.get("precision", "fp32"),
                )
            except Exception as e:  # noqa: BLE001 - best-effort sweep; one bad checkpoint mustn't abort the rest
                print(f"Engine build failed for {os.path.basename(checkpoint_path)}: {e}")
        return

    build_one(
        sys.argv[1],
        int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_ACTION_DIM,
        sys.argv[3] if len(sys.argv) > 3 else "fp32",
    )


if __name__ == "__main__":
    _main()
