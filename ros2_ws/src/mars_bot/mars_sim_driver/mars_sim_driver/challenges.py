"""Sim challenge engine: declarative tasks verified against ground truth.

A challenge is a Python module in sim/challenges/ exporting CHALLENGE =
Challenge(...): a scene setup (which props to drop where), an ordered goal
checklist of predicates over ground-truth world state and robot skill events,
and an optional time limit. The engine is hosted by the world server, which
owns ground truth -- the robot stack never sees any of this (that is what
makes the verification honest):

- tick() runs on every observer state broadcast and returns the "challenge"
  block embedded in the state stream, so any frontend is a thin renderer;
- start/abort arrive as observer-channel commands (like drop_object);
- results persist to workspace/challenges.json across restarts;
- skill executions stream in from rosbridge (/brain/skill_status_update via
  SkillEventBridge) -- best-effort: with rosbridge down, world-state
  challenges still work and SkillDone goals simply never fire.

Everything challenge-side is wrapped so a broken challenge file or predicate
degrades that challenge, never the sim.

Example (sim/challenges/shepherd.py):

    from mars_sim_driver.challenges import Challenge, Drop, Goal, Near

    CHALLENGE = Challenge(
        id="shepherd",
        title="Shepherd",
        brief="Find the soccer ball and push it to the dog.",
        setup=[Drop("labrador", 2.1, -3.4, yaw_deg=90), Drop("soccer_ball", -4.5, -1.2)],
        goals=[
            Goal("Find the ball", Near("robot", "soccer_ball", 0.8)),
            Goal("Push it to the dog", Near("soccer_ball", "labrador", 1.0)),
        ],
        time_limit_s=300,
    )
"""

import importlib.util
import json
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import world

# --- world-state view handed to predicates ---

# Body-frame offset from a prop's free-joint origin to its visual CENTER.
# The human scan keeps the mesh convention "feet at the origin, head along
# body-local +y", so its raw body position is where the FEET are -- a Near()
# against it would measure to the feet of a 1.7m body. Ball and dog are
# bbox-centered and need no correction.
OBJECT_CENTER_OFFSET = {"human": (0.0, 0.864, 0.0)}


def _center_xy(pose: list[float], offset: tuple[float, float, float]) -> tuple[float, float]:
    """xy of origin + R(quat) @ offset for a [x,y,z,qw,qx,qy,qz] pose."""
    w, qx, qy, qz = pose[3:7]
    ox, oy, oz = offset
    # Rows 0/1 of the quaternion rotation matrix; z is irrelevant for xy.
    rx = (1 - 2 * (qy * qy + qz * qz)) * ox + 2 * (qx * qy - w * qz) * oy + 2 * (qx * qz + w * qy) * oz
    ry = 2 * (qx * qy + w * qz) * ox + (1 - 2 * (qx * qx + qz * qz)) * oy + 2 * (qy * qz - w * qx) * oz
    return pose[0] + rx, pose[1] + ry


@dataclass
class WorldState:
    """Ground truth at one tick. Predicates read positions via pos()."""

    t: float  # sim time (the challenge timeline)
    robot: tuple[float, float, float]  # x, y, yaw
    objects: dict[str, list[float]]  # kind -> [x, y, z, qw, qx, qy, qz], dropped props only

    def pos(self, name: str) -> tuple[float, float] | None:
        """xy center of "robot" or a prop kind; None while the prop isn't
        dropped. Props with an off-center origin (the human) are corrected to
        their body center so distances mean what a reader expects."""
        if name == "robot":
            return self.robot[0], self.robot[1]
        pose = self.objects.get(name)
        if not pose:
            return None
        offset = OBJECT_CENTER_OFFSET.get(name)
        return _center_xy(pose, offset) if offset else (pose[0], pose[1])


# --- predicates (each: update(state, events) -> bool, reset() for reuse) ---


class Predicate:
    def update(self, state: WorldState, events: list[dict]) -> bool:
        raise NotImplementedError

    def reset(self) -> None:
        pass


@dataclass
class Near(Predicate):
    """xy distance between two things ("robot" or a prop kind) <= radius."""

    a: str
    b: str
    radius_m: float

    def update(self, state: WorldState, events: list[dict]) -> bool:
        pa, pb = state.pos(self.a), state.pos(self.b)
        if pa is None or pb is None:
            return False
        return math.hypot(pa[0] - pb[0], pa[1] - pb[1]) <= self.radius_m


@dataclass
class InCircle(Predicate):
    """A thing is within radius of a fixed world point (a named spot)."""

    target: str
    x: float
    y: float
    radius_m: float

    def update(self, state: WorldState, events: list[dict]) -> bool:
        p = state.pos(self.target)
        return p is not None and math.hypot(p[0] - self.x, p[1] - self.y) <= self.radius_m


@dataclass
class InRect(Predicate):
    """A thing is inside an axis-aligned world rectangle (a room, a zone)."""

    target: str
    x0: float
    y0: float
    x1: float
    y1: float

    def update(self, state: WorldState, events: list[dict]) -> bool:
        p = state.pos(self.target)
        if p is None:
            return False
        return min(self.x0, self.x1) <= p[0] <= max(self.x0, self.x1) and min(self.y0, self.y1) <= p[1] <= max(
            self.y0, self.y1
        )


@dataclass
class Hold(Predicate):
    """Inner predicate continuously true for `seconds` of sim time (dwell)."""

    inner: Predicate
    seconds: float
    _since: float | None = field(default=None, repr=False)

    def update(self, state: WorldState, events: list[dict]) -> bool:
        if not self.inner.update(state, events):
            self._since = None
            return False
        if self._since is None:
            self._since = state.t
        return state.t - self._since >= self.seconds

    def reset(self) -> None:
        self._since = None
        self.inner.reset()


@dataclass
class SkillDone(Predicate):
    """The robot completed a skill (matched against /brain/skill_status_update
    by skill_id or display name). An optional guard predicate must hold at the
    moment the completion event arrives ("bark WHILE near the dog")."""

    skill: str
    guard: Predicate | None = None

    def update(self, state: WorldState, events: list[dict]) -> bool:
        for ev in events:
            if ev.get("status") != "completed":
                continue
            if self.skill not in (ev.get("skill_id"), ev.get("skill_name")):
                continue
            if self.guard is None or self.guard.update(state, events):
                return True
        return False

    def reset(self) -> None:
        if self.guard is not None:
            self.guard.reset()


@dataclass
class AllOf(Predicate):
    preds: list[Predicate]

    def update(self, state: WorldState, events: list[dict]) -> bool:
        return all(p.update(state, events) for p in list(self.preds))

    def reset(self) -> None:
        for p in self.preds:
            p.reset()


@dataclass
class AnyOf(Predicate):
    preds: list[Predicate]

    def update(self, state: WorldState, events: list[dict]) -> bool:
        return any(p.update(state, events) for p in list(self.preds))

    def reset(self) -> None:
        for p in self.preds:
            p.reset()


# --- challenge definition ---


@dataclass
class Drop:
    """Scene setup: drop a prop (world.OBJECT_KINDS) at challenge start."""

    kind: str
    x: float
    y: float
    yaw_deg: float = 0.0


@dataclass
class Goal:
    """One checklist entry; latches once its predicate reports True."""

    label: str
    predicate: Predicate


@dataclass
class Challenge:
    id: str
    title: str
    brief: str  # shown to the user; what to tell (or do with) the robot
    setup: list[Drop]
    goals: list[Goal]  # strictly ordered: goal N is judged only after N-1
    time_limit_s: float | None = None
    reset_world: bool = True  # robot back to spawn + props re-parked on start


def load_challenges(directory: Path) -> dict[str, Challenge]:
    """Import every sim/challenges/*.py and collect its CHALLENGE. A broken
    file is skipped with a warning -- one bad challenge must not take out the
    world server."""
    found: dict[str, Challenge] = {}
    for path in sorted(directory.glob("*.py")):
        try:
            spec = importlib.util.spec_from_file_location(f"sim_challenge_{path.stem}", path)
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            challenge: Challenge = module.CHALLENGE
            found[challenge.id] = challenge
        except Exception as exc:  # noqa: BLE001
            print(f"[challenges] skipping {path.name}: {exc!r}", flush=True)
    return found


# --- engine ---


class ChallengeEngine:
    """Judges one active challenge at a time against the observer state feed.

    Thread model mirrors the world server: start()/abort() run on observer
    connection threads and take the sim lock for setup; tick() runs on the
    physics thread after state is gathered (no sim access, pure evaluation);
    post_event() may be called from any thread.
    """

    def __init__(self, sim, sim_lock: threading.Lock, challenges_dir: Path | None = None, progress_path: Path | None = None):
        self.sim = sim
        self.sim_lock = sim_lock
        self.challenges = load_challenges(challenges_dir or world.repo_root() / "sim" / "challenges")
        self.progress_path = progress_path or world.repo_root() / "workspace" / "challenges.json"
        self.progress = self._load_progress()
        self._mutex = threading.Lock()  # engine state (active challenge, events)
        self._events: list[dict] = []
        self.active: Challenge | None = None
        self.state = "running"  # of the active challenge: running | passed | failed
        self.reason = ""
        self.goal_done: list[bool] = []
        self.started_t = 0.0
        self.elapsed_s = 0.0
        if self.challenges:
            print(f"[challenges] loaded: {', '.join(self.challenges)}", flush=True)

    # -- persistence --

    def _load_progress(self) -> dict:
        try:
            return json.loads(self.progress_path.read_text())["challenges"]
        except Exception:  # noqa: BLE001 -- first run or corrupt file: start fresh
            return {}

    def _save_progress(self) -> None:
        self.progress_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.progress_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"version": 1, "challenges": self.progress}, indent=2) + "\n")
        tmp.replace(self.progress_path)

    def _record(self, challenge_id: str, result: str, time_s: float | None) -> None:
        entry = self.progress.setdefault(challenge_id, {"attempts": 0, "passed": False, "best_time_s": None})
        entry["last_result"] = result
        entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if result == "passed":
            entry["passed"] = True
            if time_s is not None and (entry["best_time_s"] is None or time_s < entry["best_time_s"]):
                entry["best_time_s"] = round(time_s, 1)
        try:
            self._save_progress()
        except OSError as exc:
            print(f"[challenges] could not write {self.progress_path}: {exc}", flush=True)

    # -- commands (observer connection threads) --

    def start(self, challenge_id: str) -> bool:
        challenge = self.challenges.get(challenge_id)
        if challenge is None:
            print(f"[challenges] start ignored: unknown id {challenge_id!r}", flush=True)
            return False
        with self._mutex:
            self.active = challenge
            self.state = "running"
            self.reason = ""
            self.goal_done = [False] * len(challenge.goals)
            self.elapsed_s = 0.0
            self._events.clear()
            for goal in challenge.goals:
                try:
                    goal.predicate.reset()
                except Exception:  # noqa: BLE001,S110 -- challenge bug; judged as-is
                    pass
            entry = self.progress.setdefault(challenge_id, {"attempts": 0, "passed": False, "best_time_s": None})
            entry["attempts"] += 1
        with self.sim_lock:
            if challenge.reset_world:
                self.sim.reset()
            for drop in challenge.setup:
                self.sim.drop_object(drop.kind, drop.x, drop.y, math.radians(drop.yaw_deg))
            started_t = float(self.sim.data.time)
        with self._mutex:
            self.started_t = started_t
        return True

    def abort(self) -> None:
        with self._mutex:
            if self.active is not None and self.state == "running":
                self._record(self.active.id, "aborted", None)
            self.active = None

    def post_event(self, event: dict) -> None:
        with self._mutex:
            if self.active is not None and self.state == "running":
                self._events.append(event)

    # -- evaluation (physics thread, after state gathering; no sim access) --

    def tick(self, t: float, pose: tuple[float, float, float], objects: dict[str, list[float]]) -> dict:
        """Advance the active challenge and return the state-stream block."""
        with self._mutex:
            challenge, events, self._events = self.active, self._events, []
            if challenge is not None and self.state == "running":
                state = WorldState(t=t, robot=pose, objects=objects)
                self.elapsed_s = max(0.0, t - self.started_t)
                try:
                    idx = self.goal_done.index(False)
                    if challenge.goals[idx].predicate.update(state, events):
                        self.goal_done[idx] = True
                except ValueError:  # no False left: all goals done
                    pass
                except Exception as exc:  # noqa: BLE001 -- challenge bug fails the run, not the sim
                    self.state, self.reason = "failed", f"challenge error: {exc!r}"
                    self._record(challenge.id, "failed", None)
                if self.state == "running":
                    if all(self.goal_done):
                        self.state = "passed"
                        self._record(challenge.id, "passed", self.elapsed_s)
                    elif challenge.time_limit_s is not None and self.elapsed_s > challenge.time_limit_s:
                        self.state, self.reason = "failed", "time limit"
                        self._record(challenge.id, "failed", None)
            return self._block(challenge)

    def _block(self, challenge: Challenge | None) -> dict:
        block = {
            "list": [
                {
                    "id": c.id,
                    "title": c.title,
                    "brief": c.brief,
                    "passed": bool(self.progress.get(c.id, {}).get("passed")),
                    "best_time_s": self.progress.get(c.id, {}).get("best_time_s"),
                    "attempts": self.progress.get(c.id, {}).get("attempts", 0),
                }
                for c in self.challenges.values()
            ],
            "active": None,
        }
        if challenge is not None:
            block["active"] = {
                "id": challenge.id,
                "state": self.state,
                "reason": self.reason,
                "elapsed_s": round(self.elapsed_s, 1),
                "time_limit_s": challenge.time_limit_s,
                "goals": [{"label": g.label, "done": done} for g, done in zip(challenge.goals, self.goal_done)],
            }
        return block


class SkillEventBridge:
    """Feeds robot skill lifecycle events into the engine: subscribes to
    /brain/skill_status_update over the sim stack's rosbridge websocket
    (127.0.0.1:9090, JSON protocol). Reconnects forever; entirely
    best-effort -- the sim never depends on it."""

    TOPIC = "/brain/skill_status_update"

    def __init__(self, engine: ChallengeEngine, url: str = "ws://127.0.0.1:9090"):
        self.engine = engine
        self.url = url
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError:
            print("[challenges] `websockets` client unavailable -- skill events disabled", flush=True)
            return
        announced = False
        while True:
            try:
                with connect(self.url, open_timeout=5) as ws:
                    ws.send(json.dumps({"op": "subscribe", "topic": self.TOPIC, "type": "std_msgs/String"}))
                    if not announced:
                        print(f"[challenges] skill events connected ({self.url})", flush=True)
                        announced = True
                    for message in ws:
                        frame = json.loads(message)
                        if frame.get("topic") == self.TOPIC:
                            self.engine.post_event(json.loads(frame["msg"]["data"]))
            except Exception:  # noqa: BLE001,S110 -- rosbridge down/restarting; retry
                pass
            time.sleep(5)
