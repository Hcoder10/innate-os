"""The REAL target agent: NemotronVoiceChatBackend.

Every other backend in this project (NemotronStackBackend included) is a
substitute standing in for the actual NemotronLabs VoiceChat model. This
one is not a substitute -- it calls the real 11B model, loaded on
squaredcube1's GPU.

WHY BLIND. The model's own published input contract is Text (prompt) +
Audio (user speech) -- there is no image input at all. wants_image=False is
not a scope choice made for convenience, it is what the model actually is.
Comparing its scores against a vision-equipped backend on a task that needs
object-grounding is not a fair comparison; the fair comparisons are (a)
category-1 conversation-only challenges, where sight is not the bottleneck,
and (b) its own gap against a blind LLM backend, if one exists.

WHY HTTP, NOT IN-PROCESS. The model's dependency stack cannot share a
Python environment with this harness's own sim/.venv, so it runs behind
nemotron_vc_server.py -- committed beside this file so the whole path is
reviewable, but RUN on the GPU host next to the model weights (its
constants are that host's paths). This backend is a thin client, the same
shape GeminiBackend already uses to reach a remote model -- just pointed
at squaredcube1 instead of Google.

WHAT'S ACTUALLY BEING MEASURED. The server synthesizes each turn's
observation text into speech (neural TTS -- see the server's synthesis
note for why espeak was disqualified: the model's speech front-end
returned empty markup for formant-synthesized audio while understanding
real recordings fluently) because the model's audio channel is where real
turn content goes; the text channel is system-prompt-only per its own
contract. TTS standing in for a live speaker is a real, disclosed source
of possible degradation relative to a spoken deployment. Not measuring
around it would be the same mistake NemotronStackBackend's own docstring
warns against: report what was actually run, not what the real
architecture would ideally get.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from backends import _coerce


class NemotronVoiceChatBackend:
    """HTTP client for nemotron_vc_server.py running on the remote GPU host.

    No camera, so no per-turn image cost. think_charge_s charges the TARGET
    architecture's latency, not this substitute path's -- see its note.
    """

    wants_image = False
    # Charges the model's published ~450 ms live turn-taking latency, NOT
    # the measured 148-192 s a harness-style turn takes through the offline
    # reference inference path (probes 2026-08-23; that path recomputes full
    # history each autoregressive step -- no KV cache -- which is an
    # implementation artifact of the offline script, not the architecture).
    # This is the SAME reasoning runner.py's think-charge already applies to
    # claude_bridge and nemotron_stack applies to its two HTTPS calls:
    # charge what the real deployment would pay, record wall_s separately.
    # Charging the artifact would clock every episode out at ~2 turns and
    # measure the reference script, not the agent.
    think_charge_s = 0.5

    # Default timeout must clear the measured 148-192 s harness-turn round
    # trip (long utterances decode longer; the 85 s demo recording took
    # 804 s) with margin -- 180 s sat INSIDE the normal range and would
    # have killed healthy turns.
    def __init__(self, base_url: str | None = None, timeout_s: float = 600.0):
        self.base = (base_url or os.environ.get("NEMOTRON_VC_SERVER_URL", "")).strip()
        if not self.base:
            raise RuntimeError(
                "NEMOTRON_VC_SERVER_URL is not set; point it at "
                "nemotron_vc_server.py's host:port (e.g. http://squaredcube1:8099)"
            )
        self.timeout_s = timeout_s

    def reset(self) -> None:
        # The server holds one loaded model + one encoded system prompt for
        # its whole process lifetime -- there is no per-episode state on the
        # client side to clear. (The system prompt has no episode-specific
        # content: the task brief itself is part of obs.as_text(), sent
        # fresh every turn, same as every other backend in this harness.)
        pass

    # menu is unused: the server's tool definitions already say everything
    # ACTIONS_BLIND's free text would, and injecting both into a turn that
    # gets synthesized to speech would just lengthen the audio for nothing.
    def decide(self, obs, menu) -> dict:  # noqa: ARG002
        body = json.dumps({"observation_text": obs.as_text()}).encode()
        req = urllib.request.Request(f"{self.base}/decide", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"nemotron_vc_server returned {exc.code}: {detail[-300:]}") from exc
        if "error" in data:
            raise RuntimeError(f"nemotron_vc_server error: {data['error']}")
        return _coerce(data)


BACKENDS_V4 = {"nemotron_voicechat": NemotronVoiceChatBackend}
