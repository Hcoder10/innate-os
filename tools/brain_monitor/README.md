# Brain Monitor

A single-file, zero-dependency dashboard that watches the local Gemini brain
think in real time. Open [brain_monitor.html](brain_monitor.html) in a browser
— no build, no server, no CDN.

```
open tools/brain_monitor/brain_monitor.html          # then type the robot host
brain_monitor.html?host=mars-the-26th.local          # auto-connect
brain_monitor.html?demo                              # synthetic brain, no robot
brain_monitor.html?cam=/other/image/topic            # override the live-camera topic
```

It connects straight to the robot's rosbridge (`ws://<host>:9090`) speaking the
rosbridge JSON protocol — no roslib bundle.

## What it shows

| Panel | Source | What you learn |
|---|---|---|
| **The agent loop** | `/brain/trace` | Which phase the brain is in right now — LOOK (aqua flash + camera sweep), THINK (violet pulse, orbiting comet, live seconds), ACT (orange flash) — plus the countdown ring to the next look, the running skill, and error/backoff state with the retry timer and failure streak. |
| **Robot vision** | `/brain/trace` frames | The **exact frame the model saw for this turn** (not just the live feed), with a crosshair ping where the model pointed for `go_to_point`, plus live pose and heading. Falls back to the live camera topic when trace isn't available. |
| **Mind stream** | `/brain/chat_out` | Thought summaries, spoken replies (with a live voice indicator from `/tts/is_playing`), and system messages, newest first. |
| **Turns & actions** | `/brain/trace`, `/brain/skill_status_update` | One row per turn: think latency and every tool call with its args and outcome (hover a chip for the full outcome string). Skill runs live-update running → completed/failed/interrupted. |
| **Event queue** | `/brain/trace` snapshots | What the next look will carry — pending user messages, skill results, feedback. Cards drain into the loop when a turn starts. |
| **Vitals** | `/brain/trace` | Think-latency sparkline (hover for per-turn values), turn count, last/avg latency, uptime, and the conversation-history gauge against the prune limit. |

## The trace topic

Deep telemetry comes from `/brain/trace` (`std_msgs/String`, JSON), published
by `BrainAgent` in `brain_client`. Events:

- `turn_start` — turn number, the full observation text, image count, armed
  tool names, history length, and the turn's camera frame (base64 JPEG)
- `turn_end` — think latency, thoughts, speech, every tool call with args and
  outcome, history length, seconds until the next look
- `turn_error` — the error, failure streak, and backoff
- `turn_dropped` — a turn finished after a reset/deactivation and was discarded
- `event` — something was queued for the next turn (user message, skill result)
- `snapshot` — 1 Hz heartbeat: active, backend, model, in-flight state and
  thinking time, queued events, next-turn countdown, running skill, history
  size, uptime

Robots without the trace topic (pre-local-brain builds) still get chat, skill
status, agent status, pose, and the live camera — the deep panels just stay
quiet.
