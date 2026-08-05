"""Soccer ball: scenery the robot pushes around (not the arm's stress ball)."""

from mars_sim_driver.props import Prop

# Collides as a sphere primitive rather than its mesh: a sphere rolls exactly
# right and a convexified mesh does not. condim 6 adds rolling friction, so a
# nudged ball rolls and gently stops instead of rolling forever.
PROP = Prop(
    name="soccer_ball",
    label="⚽",
    title="Soccer ball",
    mesh="../assets/objects/soccer_ball.obj",
    collision="sphere",
    size=(0.11,),  # regulation
    density=80,  # ~0.43kg at that radius
    condim=6,
    friction=(0.7, 0.01, 0.002),
    solref=(0.02, 1.0),
    margin=0.007,
    rgba=(0.9, 0.9, 0.9, 1.0),
    rest_z=0.11,
    drop_z=0.6,
    reach=(1.2, -0.8),  # offset laterally so a whole-group drop lands them side by side
    viewer={
        "glb": "/models/soccer_ball.glb",
        "rotateToZUp": True,
        "fitSizeM": 0.22,
        "fitDim": "max",
        "origin": "center",
    },
)
