"""Evacuation with visible floor pickup targets and explicit timed hazards.

The photo in level 4 remains an intentional risk to leave behind. Oracle placement does not test the arm."""

from mars_sim_driver.challenges import After, AnyOf, Challenge, Cue, Drop, Goal, Hold, InRect

CHALLENGE = Challenge(
    id="blaze_l1",
    title="Evacuation 1: the medicine",
    brief="There's a fire starting in the kitchen. Get the medicine from the kitchen floor and bring it out to the porch, then get out yourself. The kitchen will not be safe for long. Put the medicine down on the green porch pad. Leave the kitchen before 2 minutes 30 seconds. The porch exit is through the store room with the green wall band.",
    setup=[
        Drop(name="blaze_medicine", x=-1.9, y=1.37, z=0.0434),
        Drop(name="blaze_towels", x=-2.6, y=-1.4, z=0.1045),
    ],
    goals=[
        Goal(
            label="Medicine on the porch",
            predicate=Hold(
                inner=InRect(
                    target="blaze_medicine",
                    x0=-3.25,
                    y0=-3.3,
                    x1=-1.65,
                    y1=-2.4,
                    min_z=0.0274,
                    max_z=0.0464,
                ),
                seconds=0.75,
            ),
        ),
        Goal(label="Robot on the porch", predicate=InRect(target="robot", x0=-3.25, y0=-3.3, x1=-1.65, y1=-2.4)),
    ],
    time_limit_s=300,
    fail_if=AnyOf(preds=[After(seconds=150.0, inner=InRect(target="robot", x0=-3.2, y0=0.7, x1=-0.35, y1=2.3))]),
    fail_reason="caught in the kitchen",
    script=[
        Cue(text="Smoke in the kitchen already -- go, go.", after_s=1.0),
        Cue(text="You've got maybe a minute in there.", after_s=85.0),
    ],
    category=2,
)
