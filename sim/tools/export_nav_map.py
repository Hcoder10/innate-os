"""Export the apartment occupancy map as a nav2 map_server map
(sim_apartment.yaml + .pgm) into sim/assets/map/. The tmux launch script
seeds it into $INNATE_OS_ROOT/data/maps so the mode manager boots straight
into navigation mode with it.

Usage: cd sim && uv run tools/export_nav_map.py
"""

import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sandbox"))
import _driver_pkg  # noqa: F401
from mars_sim_driver.core import VirtualMars

RESOLUTION = 0.05


def main() -> None:
    sim = VirtualMars()
    # Lidar-consistent map (virtual SLAM at the laser's true height): AMCL
    # localizes against what the lidar actually returns, exactly like a real
    # robot localizing against its own SLAM map. occupancy_grid() (collision
    # slab) systematically disagrees with the lidar around furniture and
    # walks AMCL off the map.
    grid, ox, oy = sim.lidar_occupancy_grid(RESOLUTION)
    # map_server PGM: 254 free, 0 occupied, 205 unknown; row 0 at the TOP.
    img = np.where(grid == 100, 0, np.where(grid == 0, 254, 205)).astype(np.uint8)[::-1]
    out = Path(__file__).resolve().parents[1] / "assets" / "map"
    out.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(out / "sim_apartment.pgm")
    (out / "sim_apartment.yaml").write_text(
        f"image: sim_apartment.pgm\nmode: trinary\nresolution: {RESOLUTION}\n"
        f"origin: [{ox:.4f}, {oy:.4f}, 0.0]\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.25\n"
    )
    print(f"wrote {out}/sim_apartment.yaml ({grid.shape[1]}x{grid.shape[0]} @ {RESOLUTION}m)")


if __name__ == "__main__":
    main()
