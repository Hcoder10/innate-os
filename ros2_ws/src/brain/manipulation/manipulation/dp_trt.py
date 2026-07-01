# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""TensorRT acceleration for Diffusion Policy inference.

DP's cost splits in two, so it gets two engines (unlike act_trt's single graph):

- **U-Net** (``ConditionalUnet1D``) -- the DDIM sampler calls it ``num_inference_steps``
  times per replan (torch-eager ~35-70 ms each on Orin), so it dominates. Swapped in at
  DP's ``_unet_forward`` hook: one engine call replaces one eager U-Net call, sampler
  loop unchanged.
- **Obs-encoder** (2x ResNet18 + state) -- runs ONCE per replan to produce ``global_cond``,
  reused across every denoise step. It gets its OWN engine, not folded into the U-Net
  engine: folding would recompute the ResNets ``num_inference_steps`` times.

The DDIM loop itself stays in torch on purpose -- baking it into an engine would either
hard-code ``num_inference_steps`` (unrolled) or need fragile ONNX ``Loop`` support, while
saving ~nothing (the sampler step is <1 ms of elementwise ops; the U-Net is the cost).
Normalization is baked into each engine (encoder bakes obs mean/std; the U-Net is
stats-independent). Action unnormalization stays in torch.

Requires ``tensorrt`` (JetPack) and, for the one-time build only, ``onnx``. Callers
import this lazily and fall back to eager torch on ImportError.
"""

import os

import tensorrt as trt
import torch
import torch.nn as nn


# ── ONNX export wrappers ─────────────────────────────────────────────
class _UNetExportWrapper(nn.Module):
    """ConditionalUnet1D -> plain positional tensors: (sample, timestep, global_cond) -> eps.

    timestep is a (1,) float tensor (skips the U-Net's int->tensor branch); step values
    0..num_train_timesteps are exact in fp32.
    """

    def __init__(self, unet):
        super().__init__()
        self.unet = unet

    @torch.no_grad()
    def forward(self, sample, timestep, global_cond):
        return self.unet(sample, timestep, global_cond=global_cond)


class _EncoderExportWrapper(nn.Module):
    """normalize_inputs -> _encode_obs as one graph: raw (img1, img2, state) -> global_cond.

    Bakes the obs mean/std into the engine, so the runtime skips torch normalization on
    the encode path.
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
        return self.policy._encode_obs(self.policy.normalize_inputs(batch))


# ── engine cache path ────────────────────────────────────────────────
def engine_path_for(checkpoint_path: str, kind: str, dims_tag: str, precision: str = "fp16") -> str:
    """Deterministic, version-scoped engine cache path next to the checkpoint.

    ``kind`` is ``unet`` or ``encoder``. The engine bakes in weights (from the checkpoint)
    and I/O shapes, so the TRT version, precision, and every shape-determining dim
    (``dims_tag``) are in the filename: a TRT upgrade, precision change, or a checkpoint
    resolved with different dims transparently rebuilds instead of reusing a mismatched
    engine. Not keyed by dataset_stats -- the encoder bakes stats but a stats change means
    a different checkpoint/run in practice; delete the engine to force a rebuild if you
    regenerate stats in place. abspath so pre-build and runtime agree on the name.
    """
    checkpoint_path = os.path.abspath(os.path.expanduser(checkpoint_path))
    return f"{checkpoint_path}.dp_{kind}.{precision}.trt{trt.__version__}.{dims_tag}.bs1.engine"


def _timing_cache_path() -> str:
    """Shared kernel-timing cache (per TRT version / GPU); DP engines of one kind share an
    architecture, so builds after the first are much faster."""
    return os.path.expanduser(f"~/.cache/innate_trt/dp.trt{trt.__version__}.timing.cache")


# ── engine build (shared) ────────────────────────────────────────────
def _build_serialized(onnx_path: str, precision: str, log) -> bytes:
    """Parse an ONNX file and build a serialized TRT engine (fixed shapes, batch 1)."""
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
    # DP checkpoints of the same architecture. GPU/TRT-version specific; failures here
    # just fall back to a full build.
    timing_cache = None
    cache_path = _timing_cache_path()
    try:
        prev = b""
        if os.path.exists(cache_path):
            with open(cache_path, "rb") as f:
                prev = f.read()
        timing_cache = config.create_timing_cache(prev)
        config.set_timing_cache(timing_cache, ignore_mismatch=False)
    except Exception as e:  # noqa: BLE001
        log(f"[dp_trt] timing cache unavailable ({e}); building without it")

    serialized = builder.build_serialized_network(network, config)
    if serialized is None:
        raise RuntimeError("TensorRT engine build returned None")

    if timing_cache is not None:
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "wb") as f:
                f.write(timing_cache.serialize())
        except OSError:
            pass
    return serialized


def _export_and_build(wrapper, dummy, input_names, output_names, engine_path, precision, log) -> str:
    """Export ``wrapper`` to ONNX and build a cached engine at ``engine_path`` (idempotent)."""
    if os.path.exists(engine_path):
        return engine_path

    import onnx  # noqa: F401 - build-time only, validates the exported graph

    # Per-process scratch so two concurrent builds of the same checkpoint can't truncate
    # each other's ONNX mid-parse; the final write is atomic (os.replace) and both builds
    # produce an identical engine, so last-writer-win is race-free.
    scratch_prefix = f"{engine_path}.{os.getpid()}"
    onnx_path = scratch_prefix + ".onnx"
    tmp_path = scratch_prefix + ".tmp"
    try:
        log(f"[dp_trt] exporting ONNX -> {os.path.basename(engine_path)} ...")
        torch.onnx.export(
            wrapper, dummy, onnx_path,
            input_names=input_names, output_names=output_names,
            opset_version=17, do_constant_folding=True, dynamo=False,
        )
        onnx.checker.check_model(onnx.load(onnx_path))
        log(f"[dp_trt] building {precision} engine (one-time, can take 1-2 min) ...")
        serialized = _build_serialized(onnx_path, precision, log)
        # Atomic write: a process killed mid-write must not leave a truncated engine
        # (which would poison the cache -- next load skips the rebuild, fails forever).
        with open(tmp_path, "wb") as f:
            f.write(serialized)
        os.replace(tmp_path, engine_path)
    finally:
        for scratch in (onnx_path, tmp_path):
            try:
                os.remove(scratch)
            except OSError:
                pass
    log(f"[dp_trt] engine cached at {engine_path}")
    return engine_path


def _cond_dim(policy) -> int:
    return policy.obs_encoder.output_dim * policy.config.n_obs_steps


def build_unet_engine(policy, checkpoint_path: str, device, precision: str = "fp16", log=print) -> str:
    cfg = policy.config
    action_dim, horizon, cond_dim = cfg.action_dim, cfg.horizon, _cond_dim(policy)
    engine_path = engine_path_for(checkpoint_path, "unet", f"a{action_dim}.h{horizon}.g{cond_dim}", precision)
    dummy = (
        torch.zeros(1, horizon, action_dim, device=device),
        torch.zeros(1, device=device),
        torch.zeros(1, cond_dim, device=device),
    )
    wrapper = _UNetExportWrapper(policy.unet).eval().to(device)
    return _export_and_build(wrapper, dummy, ["sample", "timestep", "global_cond"], ["eps"],
                             engine_path, precision, log)


def build_encoder_engine(policy, checkpoint_path: str, device, precision: str = "fp16", log=print) -> str:
    cfg = policy.config
    cond_dim = _cond_dim(policy)
    image_shape = cfg.input_shapes[cfg.image_keys[0]]
    state_dim = cfg.input_shapes[cfg.state_keys[0]][0]
    c, h, w = image_shape
    engine_path = engine_path_for(checkpoint_path, "encoder", f"g{cond_dim}.i{c}x{h}x{w}.s{state_dim}", precision)
    dummy = (
        torch.zeros(1, *image_shape, device=device),
        torch.zeros(1, *image_shape, device=device),
        torch.zeros(1, state_dim, device=device),
    )
    wrapper = _EncoderExportWrapper(policy).eval().to(device)
    return _export_and_build(wrapper, dummy, ["image_camera_1", "image_camera_2", "state"], ["global_cond"],
                             engine_path, precision, log)


# ── engine runners ───────────────────────────────────────────────────
class _TRTRunner:
    """Deserialize an engine, validate its I/O against fixed buffers, bind addresses once."""

    def __init__(self, engine_path: str, buffers: dict, device):
        self.device = device
        self._buffers = buffers
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
        # set_tensor_address does no size checking; validate every I/O tensor against our
        # buffers so a stale engine built for a different arch fails here, not out of bounds.
        for i in range(self._engine.num_io_tensors):
            name = self._engine.get_tensor_name(i)
            if name not in buffers:
                raise RuntimeError(f"Engine I/O tensor {name!r} has no matching buffer (engine/arch mismatch)")
            expected = tuple(buffers[name].shape)
            actual = tuple(self._engine.get_tensor_shape(name))
            if actual != expected:
                raise RuntimeError(
                    f"Engine tensor {name!r} shape {actual} != expected {expected}; "
                    "cached engine was built for a different architecture"
                )
            self._context.set_tensor_address(name, buffers[name].data_ptr())

    def _execute(self, inputs: dict):
        # Wait for the caller's stream (where inputs were produced) before copying and
        # launching, so the engine can't read the buffers before the copies land.
        self._stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(self._stream):
            for name, val in inputs.items():
                self._buffers[name].copy_(val)
            ok = self._context.execute_async_v3(self._stream.cuda_stream)
        self._stream.synchronize()
        if not ok:
            raise RuntimeError("TensorRT execute_async_v3 failed (engine execution did not launch)")

    def __del__(self):
        # TensorRT teardown order: context -> engine -> runtime. Python doesn't guarantee
        # attribute GC order, so drop them explicitly (runs on every policy reload).
        for attr in ("_context", "_engine", "_runtime"):
            try:
                delattr(self, attr)
            except Exception:  # noqa: BLE001 - best-effort; attrs may be absent if __init__ failed
                pass


class TRTUNet(_TRTRunner):
    """Callable ``(sample, timestep, global_cond) -> eps`` replacing _unet_forward."""

    def __init__(self, engine_path, action_dim, horizon, cond_dim, device):
        buffers = {
            "sample": torch.empty(1, horizon, action_dim, device=device),
            "timestep": torch.empty(1, device=device),
            "global_cond": torch.empty(1, cond_dim, device=device),
            "eps": torch.empty(1, horizon, action_dim, device=device),
        }
        super().__init__(engine_path, buffers, device)

    @torch.no_grad()
    def __call__(self, sample, timestep, global_cond):
        # timestep is a python int (from sampler.timesteps); feed a (1,) float tensor.
        ts = torch.full((1,), float(timestep), device=self.device)
        self._execute({"sample": sample, "timestep": ts, "global_cond": global_cond})
        return self._buffers["eps"].clone()


class TRTEncoder(_TRTRunner):
    """Callable ``(batch) -> global_cond`` replacing normalize_inputs + _encode_obs."""

    def __init__(self, engine_path, cond_dim, image_shape, state_dim, device):
        buffers = {
            "image_camera_1": torch.empty(1, *image_shape, device=device),
            "image_camera_2": torch.empty(1, *image_shape, device=device),
            "state": torch.empty(1, state_dim, device=device),
            "global_cond": torch.empty(1, cond_dim, device=device),
        }
        super().__init__(engine_path, buffers, device)

    @torch.no_grad()
    def __call__(self, batch):
        self._execute({
            "image_camera_1": batch["observation.image_camera_1"],
            "image_camera_2": batch["observation.image_camera_2"],
            "state": batch["observation.state"],
        })
        return self._buffers["global_cond"].clone()


def attach_engines(policy, checkpoint_path: str, device, precision: str = "fp16", log=print):
    """Build (or reuse cached) both engines and attach them to ``policy``.

    Sets ``policy._trt_encoder`` and ``policy._trt_unet`` so predict_action uses them.
    Raises on any build/deserialize failure -- the DP loader catches and falls back to
    eager torch.
    """
    cfg = policy.config
    cond_dim = _cond_dim(policy)
    image_shape = cfg.input_shapes[cfg.image_keys[0]]
    state_dim = cfg.input_shapes[cfg.state_keys[0]][0]

    enc_path = build_encoder_engine(policy, checkpoint_path, device, precision, log)
    unet_path = build_unet_engine(policy, checkpoint_path, device, precision, log)
    policy._trt_encoder = TRTEncoder(enc_path, cond_dim, image_shape, state_dim, device)
    policy._trt_unet = TRTUNet(unet_path, cfg.action_dim, cfg.horizon, cond_dim, device)


def _main():
    """Offline pre-build:  python -m manipulation.dp_trt <checkpoint.pth> [action_dim=10] [fp16|fp32]"""
    import sys

    from manipulation.DP import load_dp_policy  # noqa: PLC0415

    checkpoint_path = sys.argv[1]
    action_dim = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    precision = sys.argv[3] if len(sys.argv) > 3 else "fp16"

    device = torch.device("cuda")
    stats_path = os.path.join(os.path.dirname(checkpoint_path), "dataset_stats.pt")
    dataset_stats = torch.load(stats_path, map_location="cpu") if os.path.exists(stats_path) else None
    policy, _ = load_dp_policy(checkpoint_path, dataset_stats, action_dim=action_dim, device=device)
    print("encoder:", build_encoder_engine(policy, checkpoint_path, device, precision))
    print("unet:   ", build_unet_engine(policy, checkpoint_path, device, precision))


if __name__ == "__main__":
    _main()
