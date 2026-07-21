"""Victory Lap: celebrate good news with a lap."""

from mars_sim_driver.challenges import Challenge, Goal, SkillDone

CHALLENGE = Challenge(
    id="victory_lap",
    title="Victory Lap",
    brief=(
        "Tell the robot some cheerful news in the agent chat (e.g. \"We won the "
        "championship!\"). When it hears the good news, it should celebrate by "
        "running its victory_lap skill."
    ),
    setup=[],
    goals=[
        Goal("Run a victory lap to celebrate", SkillDone("victory_lap")),
    ],
    time_limit_s=300,
)
