#!/usr/bin/env python3
"""HTTP inference server for the REAL NemotronLabs VoiceChat 11B model.

WHY A SERVER, NOT AN IN-PROCESS BACKEND. This model's dependency stack
(nemo_toolkit, its transformers/torch build, mamba-ssm compiled against that
exact torch) lives in a conda env on squaredcube1, separate from and
incompatible with the harness's own sim/.venv (mujoco, the mars sim driver).
So the model loads ONCE here, on squaredcube1's GPU, and the harness talks
to it over HTTP -- the same client shape NemotronStackBackend already uses
to reach Gemini, pointed at a local port instead of Google.

ONE PASS, NOT TWO. The reference run_fc_offline_inference does two full
inference passes: pass 1 detects the tool call, pass 2 re-runs everything
to decode the agent's spoken audio with the tool response injected. The
harness consumes the ACTION, never the speech, so this server runs only
pass 1 (decode_audio=False) and extracts the tool call itself with the
same parsing the reference applies to pass 1's output. Half the per-turn
GPU work of the reference path, and no placeholder "api_response" object
to satisfy a signature whose response-injection machinery is never wanted.

Usage: python3 nemotron_vc_server.py [--port 8099]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, "/home/sarta/Speech")

from nemo.collections.speechlm2.inference.utils.offline_voicechat import (  # noqa: E402
    build_model,
    encode_system_prompt,
    load_wav_16k_mono,
    render_fc_system_prompt,
    run_offline_inference,
)

CHECKPOINT = os.environ.get("NVC_CHECKPOINT", "/home/sarta/models/NemotronLabs-VoiceChat-11B")
TEMPLATE = os.environ.get("NVC_TEMPLATE", "/home/sarta/Speech/examples/speechlm2/function_calling/template.jinja")

# The 7 blind-harness actions (no "look" -- this model has no camera) as
# tool definitions in the shape the FC template renders. Names and argument
# shapes match brain_agent.py's ACTIONS_BLIND exactly, so a returned tool
# call maps onto the harness's {"action", "args"} schema with no
# translation layer to get wrong.
TOOLS = [
    {
        "function": {
            "name": "turn",
            "description": "Turn in place. Positive degrees is left.",
            "parameters": {
                "type": "object",
                "properties": {"degrees": {"type": "number", "description": "-180..180"}},
                "required": ["degrees"],
            },
        }
    },
    {
        "function": {
            "name": "forward",
            "description": "Drive straight ahead.",
            "parameters": {
                "type": "object",
                "properties": {"metres": {"type": "number", "description": "0.05..3.0"}},
                "required": ["metres"],
            },
        }
    },
    {
        "function": {
            "name": "pick",
            "description": "Grasp the nearest object within 42 cm.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }
    },
    {
        "function": {
            "name": "place",
            "description": "Put down what you are carrying, 30 cm ahead.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }
    },
    {
        "function": {
            "name": "say",
            "description": "Speak to the person.",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        }
    },
    {
        "function": {
            "name": "answer",
            "description": "Answer a question you were asked.",
            "parameters": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
        }
    },
    {
        "function": {
            "name": "finish",
            "description": "Stop; the task is done or impossible.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }
    },
]

SYSTEM_MESSAGE = (
    "You are the reasoning core of a small mobile robot, about 25 cm tall, doing "
    "what a person asks. You act one step at a time and are told the result of "
    "each step before choosing the next. YOU HAVE NO CAMERA -- you cannot see "
    "anything, identify objects, count them, or read colours. Your arm only "
    "reaches things below about 30 cm and you cannot climb.\n\n"
    "You MUST call exactly one of the tools listed below every single turn -- "
    "this is a decision loop, not a chat; a turn where you reply without "
    "calling a tool wastes it. Never invent a tool not listed."
)


# Neural TTS, not espeak. The conformer front-end was trained on human
# speech and cannot parse espeak's formant synthesis at all -- diagnostic
# 2026-08-23: the same pipeline that understood a real recording fluently
# (and recited this server's own tool list back) returned empty timestamp
# markup for every espeak input, long or short. edge-tts needs network
# (Microsoft's TTS endpoint); a failure raises and surfaces as a 500 on
# that turn rather than silently feeding the model unparseable audio.
EDGE_TTS = os.environ.get("NVC_EDGE_TTS", "/home/sarta/nemotron-voicechat-dl-venv/bin/edge-tts")
TTS_VOICE = "en-US-AriaNeural"


def _synthesize_wav(text: str) -> str:
    # .mp3 for honesty: edge-tts emits mp3 regardless of the extension
    # asked for, and load_wav_16k_mono decodes+resamples it fine.
    path = f"/tmp/nvc_turn_{uuid.uuid4().hex}.mp3"
    subprocess.run(
        [EDGE_TTS, "--voice", TTS_VOICE, "--text", text, "--write-media", path],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return path


def _extract_tool_calls(model, result: dict) -> list[dict]:
    """Pass-1 tool-call extraction, same parsing the reference applies.

    Returns [{"name", "arguments"}] -- empty when the model made no call.
    Malformed JSON inside a <TOOLCALL> block is dropped rather than raised:
    a garbled call and no call at all get the same fallback downstream.
    """
    func_tokens = result.get("tokens_function_pred", result.get("tokens_function"))
    if func_tokens is None:
        return []
    positions = model.stt_model._extract_function_call_positions(
        func_tokens, result.get("tokens_len"), result.get("tokens_text")
    )
    calls: list[dict] = []
    for b_info in positions:
        for call in b_info.get("function_calls", []):
            clean = call["call_text"].replace("<SPECIAL_20>", "").replace("<SPECIAL_21>", "").strip()
            if "<TOOLCALL>" in clean:
                clean = clean.split("<TOOLCALL>")[1].split("</TOOLCALL>")[0].strip()
            try:
                parsed = json.loads(clean) if clean.startswith("[") else [json.loads(clean)]
            except json.JSONDecodeError:
                continue
            for tc in parsed:
                args = tc.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                calls.append({"name": str(tc.get("name", "")), "arguments": args})
    return calls


def _decide(model, prompt_tokens, prompt_token_lens, observation_text: str, wav_path: str | None = None) -> dict:
    # wav_path bypasses TTS entirely -- diagnostic seam for feeding real
    # recorded speech (e.g. the checkpoint's own demo wavs) to separate
    # "the FC pipeline is broken" from "the model can't parse espeak audio".
    own_wav = wav_path is None
    if own_wav:
        # Inside the try below, so a synthesis that dies part-way still has
        # its half-written file removed by the same finally.
        wav_path = None
    try:
        if own_wav:
            wav_path = _synthesize_wav(observation_text)
        _, input_signal, input_signal_lens = load_wav_16k_mono(wav_path, device="cuda")
        result = run_offline_inference(
            model,
            input_signal=input_signal,
            input_signal_lens=input_signal_lens,
            prompt_tokens=prompt_tokens,
            prompt_token_lens=prompt_token_lens,
            decode_audio=False,
        )
    finally:
        # wav_path is None when synthesis itself failed; there is nothing to
        # remove in that case, and Path(None) would mask the real exception.
        if own_wav and wav_path:
            Path(wav_path).unlink(missing_ok=True)

    text = result.get("text", [""])[0]
    calls = _extract_tool_calls(model, result)
    if calls:
        return {"action": calls[0]["name"], "args": calls[0]["arguments"], "raw_text": text, "tool_calls": calls}
    # No (parseable) tool call: relay the model's text as a spoken turn
    # rather than dropping it -- the same graceful degradation backends.py's
    # _coerce applies to malformed replies. "say" is always a legal action.
    return {"action": "say", "args": {"text": text or "(no response)"}, "raw_text": text, "tool_calls": []}


class Handler(BaseHTTPRequestHandler):
    # HTTPServer (not the Threading variant) on purpose: one CUDA model,
    # one request at a time. The harness is a sequential turn loop, so
    # serialization costs nothing and removes the shared-model race a
    # threaded server would silently allow.

    def do_POST(self):  # noqa: N802
        if self.path != "/decide":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            # A bad request is a bad request, not a dead connection: the
            # harness retries on 400 and gives up on a dropped socket.
            self._reply(400, {"error": f"malformed request: {exc}"})
            return
        wav_path = body.get("wav_path")
        if wav_path is not None and not Path(wav_path).is_file():
            self._reply(400, {"error": f"wav_path not found: {wav_path}"})
            return
        t0 = time.time()
        try:
            decision = _decide(
                self.server.model,
                self.server.prompt_tokens,
                self.server.prompt_token_lens,
                body.get("observation_text", ""),
                wav_path=wav_path,
            )
            decision["elapsed_s"] = round(time.time() - t0, 2)
            self._reply(200, decision)
        except Exception as exc:  # noqa: BLE001 -- one bad turn must not kill the server
            print(f"ERROR: {type(exc).__name__}: {exc}", flush=True)
            self._reply(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def _reply(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print(f"[http] {fmt % args}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    # Explicit rather than baked in: the harness reaches this over Tailscale
    # from another host, so the default has to be the wildcard, but a local
    # run should be able to say so.
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args()

    print("Building model (loads ~44GB of weights)...", flush=True)
    model = build_model(CHECKPOINT, device="cuda")
    print("Model ready.", flush=True)
    system_prompt = render_fc_system_prompt(TEMPLATE, SYSTEM_MESSAGE, TOOLS)
    prompt_tokens, prompt_token_lens = encode_system_prompt(model, system_prompt, device="cuda")
    print(f"System prompt encoded ({len(system_prompt)} chars).", flush=True)

    server = HTTPServer((args.bind, args.port), Handler)
    server.model = model
    server.prompt_tokens = prompt_tokens
    server.prompt_token_lens = prompt_token_lens
    print(f"Listening on :{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
