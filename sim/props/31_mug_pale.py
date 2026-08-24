"""Pale mug: 30_mug.py with the parquet's own colour, and nothing else changed.

The low-contrast half of the contrast pair. pick_any_object tracks the object
between Gemini looks with a CamShift model over an HSV histogram
(_BlobTracker, WRIST_SEG_MIN_SCORE=25); 12_sock.py and 13_bar.py both record
that a prop the colour of the floor starves that tracker. Every other field is
byte-identical to the mug so a score difference across the pair can only be
colour.

rgba is the measured mean of the Quartos floor as the head camera sees it, so
"low contrast" is a measurement rather than a guess.
"""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="mug_pale",
    label="☕",
    title="Mug (pale)",
    collision="cylinder",
    size=(0.020, 0.03),
    density=1050,
    condim=4,
    friction=(1.0, 0.02, 0.005),
    rgba=(0.62, 0.52, 0.42, 1.0),
    rest_z=0.03,
    drop_z=0.38,
    reach=(0.296, 0.011),
)
