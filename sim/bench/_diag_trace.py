import sys, json, math
sys.path.insert(0, 'sim/bench')
from runner import run_episode
from brain_agent import BrainAgent
from backends_v2 import NemotronStackBackend

map_name = sys.argv[1]
challenge = sys.argv[2]
out_path = sys.argv[3]

trace = []


def make(ch):
    a = BrainAgent(NemotronStackBackend())
    a.name = 'brain:nemotron_stack'
    # Same formula as main.py/claude_bridge.py -- without this, every
    # challenge silently runs at BrainAgent's flat 40-turn class default
    # regardless of its own time_limit_s. See FINDINGS.md T19.
    a.max_turns = max(40, int((ch.time_limit_s or 400) / 9))
    b = a.backend
    real_decide = b.decide

    def traced_decide(obs, menu):
        pose_before = obs.robot_pose
        warned = obs.blocked_streak >= 2
        reply = real_decide(obs, menu)
        entry = {
            'turn': len(trace),
            'pose_before': pose_before,
            'heading_deg_before': round(math.degrees(pose_before[2]), 1) if pose_before else None,
            'last_result': obs.last_result,
            'blocked_streak': obs.blocked_streak,
            'warned': warned,
            'action': reply,
            'goals': list(b.stack.goals),
            'facts': dict(b.stack.facts),
            'constraints': list(b.stack.constraints),
        }
        trace.append(entry)
        flag = ' [WARNED]' if warned else ''
        print(f"[{entry['turn']:02d}] hdg={entry['heading_deg_before']} streak={obs.blocked_streak}{flag} "
              f"last='{entry['last_result']}' -> {reply} "
              f"goals={b.stack.goals}", flush=True)
        return reply

    b.decide = traced_decide
    return a


ep = run_episode(map_name, challenge, make, render_wh=(160, 120))
print('\nRESULT', ep.passed, f'{ep.goals_done}/{ep.goals_total}', ep.reason)
with open(out_path, 'w') as f:
    json.dump({'result': {'passed': ep.passed, 'goals_done': ep.goals_done,
                           'goals_total': ep.goals_total, 'reason': ep.reason},
               'trace': trace}, f, indent=2)
print('Trace saved:', out_path)
