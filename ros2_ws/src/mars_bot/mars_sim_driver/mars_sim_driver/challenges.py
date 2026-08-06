"""Sim challenge engine: declarative tasks verified against ground truth.

A challenge is a Python module in sim/challenges/ exporting CHALLENGE =
Challenge(...) -- the same sidecar shape as the props it drops (props.py): a
scene setup (which props to drop where), an ordered goal checklist of
predicates over ground-truth world state and robot skill events, and an
optional time limit. The engine is hosted by the world server, which owns
ground truth -- the robot stack never sees any of this (that is what makes the
verification honest):

- tick() runs on every observer state broadcast and returns the "challenge"
  block embedded in the state stream, so any frontend is a thin renderer;
- start/abort arrive as observer-channel commands (like drop_prop_at);
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

# How far sim time may run backwards before tick() reads it as the world
# having been rebuilt under the run. Two publish_state callers (the physics
# thread and an observer command) can legitimately deliver snapshots about one
# physics slice apart, ~25ms; a reset drops the clock by the whole uptime.
CLOCK_REWIND_S = 0.5

# --- world-state view handed to predicates ---


@dataclass
class WorldState:
    """Ground truth at one tick. Predicates read positions via pos()."""

    t: float  # sim time (the challenge timeline)
    robot: tuple[float, float, float]  # x, y, yaw
    # Prop name -> xy of its visual CENTRE (PropRegistry.center_xy, which
    # applies the sidecar's center_offset: the human scan stands feet-at-
    # origin, and a Near() against it must not measure to the feet of a 1.7m
    # body). Props still parked off-map are absent, not reported.
    centers: dict[str, tuple[float, float]]

    def pos(self, name: str) -> tuple[float, float] | None:
        """xy of "robot" or a prop; None while that prop isn't dropped."""
        if name == "robot":
            return self.robot[0], self.robot[1]
        return self.centers.get(name)


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


# Every child is updated on every tick, never short-circuited: a stateful
# predicate only advances when it is asked, so a Hold sitting behind a decided
# sibling would silently restart its dwell each tick if all()/any() stopped
# early. Judge first, combine second.


@dataclass
class AllOf(Predicate):
    preds: list[Predicate]

    def update(self, state: WorldState, events: list[dict]) -> bool:
        return all([p.update(state, events) for p in self.preds])  # noqa: C419 -- see above

    def reset(self) -> None:
        for p in self.preds:
            p.reset()


@dataclass
class AnyOf(Predicate):
    preds: list[Predicate]

    def update(self, state: WorldState, events: list[dict]) -> bool:
        return any([p.update(state, events) for p in self.preds])  # noqa: C419 -- see above

    def reset(self) -> None:
        for p in self.preds:
            p.reset()


# --- challenge definition ---


@dataclass
class Drop:
    """Scene setup: drop a prop (a sidecar in sim/props/, by its `name`) at
    (x, y) when the challenge starts; physics settles it onto whatever is
    below. A name no sidecar claims is skipped with a warning."""

    name: str
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


def load_challenges(roots: list[Path]) -> dict[str, Challenge]:
    """Every challenge under `roots`, later roots overriding earlier ones by
    id -- the same shape as props.load_props, so an asset bundle can ship a
    challenge pack next to the props it needs. A broken file is skipped with a
    warning: one bad challenge must not take out the world server.

    Filenames sort the roster the user sees, so they are numbered by what the
    challenge asks for rather than by when it was written: 10s skills, 20s
    people, 30s moving things around."""
    found: dict[str, Challenge] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.py")):
            if path.name.startswith("_"):
                continue
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
    connection threads, one at a time under _run_lock, and take the sim lock
    for setup; tick() runs wherever publish_state() does -- the physics thread
    every slice, and the observer thread that just ran a command -- always
    after state gathering, with no sim access of its own (pure evaluation
    under _mutex); post_event() may be called from any thread.

    Lock order is _run_lock -> sim_lock -> _mutex, and no two are ever held
    together; anything that changes here has to keep that true.
    """

    def __init__(
        self, sim, sim_lock: threading.Lock, roots: list[Path] | None = None, progress_path: Path | None = None
    ):
        self.sim = sim
        self.sim_lock = sim_lock
        # Tracked source dir plus anything the asset bundle shipped, like the
        # props (core.VirtualMars): a pack can carry its scenarios with it.
        if roots is None:  # an explicitly empty list means "load nothing"
            roots = [world.repo_root() / "sim" / "challenges", world.default_assets_dir() / "challenges"]
        self.challenges = load_challenges(roots)
        self.progress_path = progress_path or world.repo_root() / "workspace" / "challenges.json"
        self.progress = self._load_progress()
        self._mutex = threading.Lock()  # engine state (active challenge, events)
        # Serializes whole start/abort transitions. start() is three critical
        # sections (deactivate, build the scene, publish the run) and holds
        # nothing across them, so without this two observer connections can
        # interleave and publish one challenge over the other's world. Always
        # taken OUTERMOST -- never while holding _mutex or the sim lock.
        self._run_lock = threading.Lock()
        # Which build of the world a state snapshot came from. Bumped by
        # start() at the top of its scene build and read by publish_state()
        # beside t/pose/centers -- both under the SIM lock, which is what
        # guards it. _run_epoch (ordinary _mutex state) is the epoch the
        # active run was built at: publish_state ticks AFTER releasing the sim
        # lock, so a start() completing in that gap would otherwise have its
        # fresh run judged against the previous one's clock and props.
        self.world_epoch = 0
        self._run_epoch = 0
        self._events: list[dict] = []
        self.active: Challenge | None = None
        self.state = "running"  # of the active challenge: running | passed | failed
        self.reason = ""
        self.goal_done: list[bool] = []
        self.started_t = 0.0
        self._last_judged_t = 0.0  # newest sim time judged; see CLOCK_REWIND_S
        self.elapsed_s = 0.0
        if self.challenges:
            print(f"[challenges] loaded: {', '.join(self.challenges)}", flush=True)

    # -- persistence --

    def _load_progress(self) -> dict:
        try:
            data = json.loads(self.progress_path.read_text())["challenges"]
        except Exception:  # noqa: BLE001 -- first run or corrupt file: start fresh
            return {}
        # Valid JSON of the wrong shape raises nothing here, only later where
        # the fields are read. Entries of the wrong shape are not screened out
        # field by field: world_server catches the whole challenge layer off
        # the physics thread, which covers every shape rather than the ones
        # thought of here.
        return data if isinstance(data, dict) else {}

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
        # Nothing is judged while the scene is being built. The world reset and
        # the drops take the sim lock, which the physics thread keeps grabbing
        # between ticks, so a tick lands in the middle of this -- and it must
        # not see a challenge whose start time isn't known yet (elapsed_s
        # against a stale started_t instantly "times out" a fresh run) or judge
        # goal 0 against the world the last run left behind. So: deactivate,
        # build the scene, then publish the whole run in one atomic step.
        #
        # _run_lock holds those three steps together against a SECOND starter.
        # Each observer connection commands on its own thread, so two tabs
        # starting different challenges could otherwise interleave -- A drops
        # its props, B's reset re-parks them and drops its own, and whichever
        # publishes last is judged against the other's world, with goals that
        # can never fire. Serialized, the later start simply wins: it aborts
        # the earlier run and builds its scene on top.
        with self._run_lock:
            self._deactivate()
            with self.sim_lock:
                # First, so any snapshot gathered before this build carries the
                # old epoch -- including one already sitting in publish_state's
                # gap, waiting to be ticked.
                self.world_epoch += 1
                if challenge.reset_world:
                    self.sim.reset()  # also re-parks every prop (props.py)
                for drop in challenge.setup:
                    if not self.sim.drop_prop_at(drop.name, drop.x, drop.y, math.radians(drop.yaw_deg)):
                        print(f"[challenges] {challenge.id}: no prop named {drop.name!r} in this world", flush=True)
                started_t = float(self.sim.data.time)
                epoch = self.world_epoch
            with self._mutex:
                for goal in challenge.goals:
                    try:
                        goal.predicate.reset()
                    except Exception:  # noqa: BLE001,S110 -- challenge bug; judged as-is
                        pass
                self.state = "running"
                self.reason = ""
                self.goal_done = [False] * len(challenge.goals)
                self.started_t = started_t
                self._last_judged_t = started_t
                self._run_epoch = epoch
                self.elapsed_s = 0.0
                self._events.clear()  # anything that happened during setup is not this run's
                entry = self.progress.setdefault(challenge_id, {"attempts": 0, "passed": False, "best_time_s": None})
                entry["attempts"] += 1
                self.active = challenge  # last: judging starts here
        return True

    def abort(self) -> None:
        # Same lock as start(): aborting mid-build would otherwise clear an
        # `active` the starting thread is about to overwrite anyway, leaving
        # its scene on the floor with nothing judging it.
        with self._run_lock:
            self._deactivate()

    def _deactivate(self) -> None:
        """Stop judging. A run still in progress is recorded as aborted --
        whether the user pressed Abort or started something else over it."""
        with self._mutex:
            if self.active is not None and self.state == "running":
                self._record(self.active.id, "aborted", None)
            self.active = None

    def post_event(self, event: dict) -> None:
        with self._mutex:
            if self.active is not None and self.state == "running":
                self._events.append(event)

    # -- evaluation (physics thread, after state gathering; no sim access) --

    def tick(
        self, t: float, pose: tuple[float, float, float], centers: dict[str, tuple[float, float]], epoch: int
    ) -> dict:
        """Advance the active challenge and return the state-stream block.

        `epoch` is world_epoch, read under the sim lock with t/pose/centers. A
        snapshot from before the active run's scene build carries an older one
        and is rendered but never judged: its t is the previous run's clock (a
        whole server uptime ahead of started_t once the run reset the world --
        an instant "time limit") and its centers are the previous run's props.
        Names the world the numbers came from, so it holds whether or not the
        run rewound the clock."""
        with self._mutex:
            challenge = self.active
            judging = challenge is not None and self.state == "running" and epoch == self._run_epoch
            if judging and t < self._last_judged_t - CLOCK_REWIND_S:
                # Sim time only runs backwards when something rebuilt the world
                # under the run: /virtual_mars/reset, the observer reset op, or
                # core.step()'s NaN recovery -- none of which tell the engine.
                # The run's props are parked, so no goal can pass, and
                # started_t is now ahead of the clock, so elapsed_s clamps to
                # 0.0 and the time limit can never fire either. Left alone that
                # is a 0:00 run nothing but a manual abort can end.
                self.state, self.reason = "failed", "the sim was reset"
                self._record(challenge.id, "failed", None)
                judging = False
            if judging:
                self._last_judged_t = t
                # Drained only when judged: a skipped tick must leave this
                # run's skill completions for the next current one.
                events, self._events = self._events, []
                state = WorldState(t=t, robot=pose, centers=centers)
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

    def roster(self) -> list[dict]:
        """What each challenge IS. Nothing here changes while the server runs,
        so it goes out once per observer connection (world_server.serve_state)
        rather than ~75 times a second -- the briefs are paragraphs."""
        return [{"id": c.id, "title": c.title, "brief": c.brief} for c in self.challenges.values()]

    def _block(self, challenge: Challenge | None) -> dict:
        # Only what can change rides the state stream. Progress is a few
        # numbers per attempted challenge, so it ships every tick rather than
        # in a change-only frame: the stream is latest-wins, and a client that
        # skips the one frame carrying an update would keep a stale roster.
        block = {
            "progress": {
                cid: {
                    "passed": bool(entry.get("passed")),
                    "best_time_s": entry.get("best_time_s"),
                    "attempts": entry.get("attempts", 0),
                }
                for cid, entry in self.progress.items()
                if cid in self.challenges
            },
            "active": None,
        }
        if challenge is not None:
            block["active"] = {
                "id": challenge.id,
                "state": self.state,
                "reason": self.reason,
                "elapsed_s": round(self.elapsed_s, 1),
                "time_limit_s": challenge.time_limit_s,
                "goals": [
                    {"label": g.label, "done": done} for g, done in zip(challenge.goals, self.goal_done, strict=True)
                ],
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
                        # Per message: the topic is an open std_msgs/String bus,
                        # so one malformed frame must cost that frame and not
                        # the connection -- a teardown here sleeps 5s, and
                        # rosbridge does not replay what was published meanwhile.
                        try:
                            frame = json.loads(message)
                            if frame.get("topic") == self.TOPIC:
                                self.engine.post_event(json.loads(frame["msg"]["data"]))
                        except Exception as exc:  # noqa: BLE001 -- junk on the bus; keep listening
                            print(f"[challenges] ignoring skill event: {exc!r}", flush=True)
            except Exception:  # noqa: BLE001,S110 -- rosbridge down/restarting; retry
                pass
            time.sleep(5)
