import os
import sys
sys.path.insert(0, 'sim/bench')
from runner import sources

affected = []
total = 0
for map_name, (assets, ch_root) in sources().items():
    if assets is not None:
        os.environ['VIRTUAL_MARS_ASSETS'] = str(assets)
    else:
        os.environ.pop('VIRTUAL_MARS_ASSETS', None)
    from mars_sim_driver.challenges import load_challenges
    for ch in load_challenges([ch_root]).values():
        total += 1
        t = ch.time_limit_s or 400
        old = 40
        new = max(40, int(t / 9))
        if new > old:
            affected.append((map_name, ch.id, t, old, new))

print(f'{len(affected)} of {total} challenges get MORE turns under the corrected formula:\n')
for map_name, cid, t, old, new in sorted(affected, key=lambda r: -r[4]):
    print(f'  {map_name:<10} {cid:<28} time_limit_s={t:<5} old_cap={old:<4} new_cap={new:<4} (+{new-old})')
