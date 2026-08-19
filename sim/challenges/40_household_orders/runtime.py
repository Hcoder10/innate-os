"""Private stateful behavior for the Household Orders environment."""

import math
import re
from dataclasses import dataclass

from mars_sim_driver.challenges import ChallengeRuntime, RuntimeResult, WorldState

_NON_WORD = re.compile(r"[^a-z0-9]+")
_READBACK_PREFIXES = ("actually ", "okay ", "so you want ", "you want ", "your order is ", "you said ")

# A nearby resident should only answer speech directed toward them. The head
# camera's horizontal field of view is about 84 degrees; this 50-degree half
# angle keeps a small navigation margin without letting off-camera residents
# answer for the person the robot is actually looking at.
_REPLY_HALF_ANGLE_RAD = math.radians(50.0)


@dataclass(frozen=True)
class Resident:
    """One challenge-owned person and the private order they will disclose."""

    id: str
    name: str
    prop: str
    order: str
    # Complete alternate readbacks accepted in addition to the exact order.
    # Whole-utterance matching is deliberate: independent substring checks can
    # accept an order followed by a contradiction.
    accepted_readbacks: tuple[str, ...] = ()
    radius_m: float = 1.5


def _normalize_speech(text: str) -> str:
    return " ".join(_NON_WORD.sub(" ", text.lower()).split())


class HouseholdOrdersRuntime(ChallengeRuntime):
    """Reveal, correct, and confirm resident orders during one challenge run."""

    def __init__(self, residents: list[Resident]):
        self.residents = residents
        self._shared: set[str] = set()
        self._confirmed: set[str] = set()

    def reset(self) -> None:
        self._shared.clear()
        self._confirmed.clear()

    @staticmethod
    def _matches(resident: Resident, text: str) -> bool:
        normalized = _normalize_speech(text)
        while prefix := next((prefix for prefix in _READBACK_PREFIXES if normalized.startswith(prefix)), None):
            normalized = normalized[len(prefix) :]
        accepted = (resident.order, *resident.accepted_readbacks)
        return normalized in {_normalize_speech(readback) for readback in accepted}

    @staticmethod
    def _is_in_front(robot: tuple[float, float, float], pos: tuple[float, float]) -> bool:
        dx, dy = pos[0] - robot[0], pos[1] - robot[1]
        if dx == 0.0 and dy == 0.0:
            return True
        bearing = math.atan2(dy, dx)
        relative_bearing = math.atan2(math.sin(bearing - robot[2]), math.cos(bearing - robot[2]))
        return abs(relative_bearing) <= _REPLY_HALF_ANGLE_RAD

    def _nearest(self, state: WorldState) -> Resident | None:
        robot = state.pos("robot")
        if robot is None:
            return None
        candidates: list[tuple[float, str, Resident]] = []
        for resident in self.residents:
            pos = state.pos(resident.prop)
            if pos is None:
                continue
            distance = math.hypot(robot[0] - pos[0], robot[1] - pos[1])
            if distance <= resident.radius_m and self._is_in_front(state.robot, pos):
                candidates.append((distance, resident.id, resident))
        return min(candidates, default=(0.0, "", None))[2]

    def update(self, state: WorldState, events: list[dict]) -> RuntimeResult:
        result = RuntimeResult()
        for event in events:
            if event.get("type") != "robot_speech" or not isinstance(event.get("text"), str):
                continue
            resident = self._nearest(state)
            if resident is None or resident.id in self._confirmed:
                continue
            if resident.id not in self._shared:
                self._shared.add(resident.id)
                result.replies.append(f"{resident.name}: {resident.order} Please repeat the complete order back to me.")
                continue
            if self._matches(resident, event["text"]):
                self._confirmed.add(resident.id)
                result.events.append({"type": "resident_order_confirmed", "resident": resident.id})
                result.replies.append(f"{resident.name}: That's correct. Thank you.")
            else:
                result.replies.append(
                    f"{resident.name}: Not quite. {resident.order} Please repeat the complete order back to me."
                )
        return result
