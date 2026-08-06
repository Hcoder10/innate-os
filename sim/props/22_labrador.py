"""Labrador: scenery with a real silhouette -- legs, head, and a gap underneath."""

from mars_sim_driver.props import Prop

# Collides as its true shape via the CoACD pieces the asset pipeline writes
# next to the mesh, so the legs and the gap under the belly are all live. With
# no pieces installed it degrades to the mesh's own convex hull, and with no
# mesh at all to the bbox box below (~34kg).
PROP = Prop(
    name="labrador",
    label="🐕",
    title="Dog",
    mesh="../assets/objects/labrador.obj",
    collision="pieces",
    size=(0.124, 0.5, 0.256),
    density=1000,  # flesh; ~30kg over the decomposed volume
    condim=4,
    friction=(0.9, 0.01, 0.001),
    solref=(0.02, 1.0),
    margin=0.007,
    rgba=(0.82, 0.68, 0.44, 1.0),
    rest_z=0.256,
    drop_z=1.0,
    reach=(1.2, 0.0),
    viewer={
        "glb": "/models/labrador.glb",
        "rotateToZUp": True,
        "fitSizeM": 1.0,
        "fitDim": "max",
        "origin": "center",
        # CoACD hull soup (float32 xyz) in the same body frame, for the
        # "collisions" overlay.
        "hulls": "/models/labrador_hulls.f32",
    },
)
