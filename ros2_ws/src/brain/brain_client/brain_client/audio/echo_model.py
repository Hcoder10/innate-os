# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Loader for the learned echo predictor used by barge-in detection.

The model (trained by scripts/audio/train_echo_model.py from robot-recorded
TTS/mic pairs) maps a context window of reference log-mel frames to the mic's
expected echo log-mel frame. It replaces the adaptive per-band gains, which
cannot capture the speaker's nonlinearity at high levels.

torch is an optional dependency at this call site: if it or the model file is
missing, callers fall back to the adaptive-gain baseline.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import numpy as np

CONTEXT_FRAMES = 21  # must match train_echo_model.py


def load_predictor(path: str, logger=None) -> Callable[[np.ndarray], np.ndarray] | None:
    """Return a (ctx_frames, N_MELS) band-power -> (N_MELS,) predictor, or None."""
    if not path or not os.path.isfile(path):
        return None
    try:
        import torch
    except ImportError:
        if logger:
            logger.warning(f"barge-in model {path} present but torch unavailable; using adaptive gains")
        return None
    try:
        model = torch.jit.load(path, map_location="cpu")
        model.eval()
    except Exception as e:
        if logger:
            logger.error(f"failed to load barge-in echo model {path}: {e}")
        return None

    def predict(ref_ctx: np.ndarray) -> np.ndarray:
        # pad/trim the context to the trained window, log-compress like training
        ctx = ref_ctx[-CONTEXT_FRAMES:]
        if len(ctx) < CONTEXT_FRAMES:
            ctx = np.concatenate([np.tile(ctx[:1], (CONTEXT_FRAMES - len(ctx), 1)), ctx])
        x = np.log10(ctx.astype(np.float32) + 1.0)
        with torch.no_grad():
            y = model(torch.from_numpy(x[None])).numpy()[0]
        return 10.0**y - 1.0

    if logger:
        logger.info(f"🧠 barge-in learned echo model loaded: {path}")
    return predict
