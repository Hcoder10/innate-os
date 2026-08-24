"""The 0.35 m doorway. The base footprint is 0.188 x 0.182 m, so this leaves
about 8 cm of total clearance -- inside the inflation radius a planner would
normally keep, which is what separates "won't fit" from "won't try"."""

from mars_sim_driver.challenges import Challenge, Goal, Hold, InRect

CHALLENGE = Challenge(
    id="rounds_narrow_door",
    title="Narrow door",
    category=2,
    brief="The first room off the corridor has a red door frame. Go inside it.",
    setup=[],
    goals=[Goal("Inside the red-door room", Hold(InRect("robot", -5.8, -3.6, -3.2, -1.0), 1.5))],
    time_limit_s=300,
)
