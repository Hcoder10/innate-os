"""Parquet-coloured can: the low-contrast twin of `can`."""

from mars_sim_driver.props import Prop

# rgba sampled off a rendered head frame of the bare Sala_Cozinha floor at the
# skill's own tilt (-20 deg), so the albedo is the parquet's rather than a
# guess. Everything else is 11_can.py's: only the colour separates this from
# the shipped can, which is what makes _BlobTracker's CamShift model (seeded
# from a Gemini box, kept alive by WRIST_SEG_MIN_SCORE=25) the one thing a
# difference in outcome can be attributed to.
PROP = Prop(
    name="can_pale",
    label="🥫",
    title="Can (parquet)",
    collision="cylinder",
    size=(0.020, 0.03),
    density=1050,
    condim=4,
    friction=(1.0, 0.02, 0.005),
    rgba=(0.80, 0.73, 0.61, 1.0),
    rest_z=0.03,
    reach=(0.296, 0.011),
)
