#!/usr/bin/env bash
# Speak one instruction to the robot. Takes the text on stdin.
#
# Shelled out to ros2 rather than published over rosbridge: the rosbridge
# advertise+publish returned no error and the brain never logged a User message
# (grep "User message" == 0), while this path has worked every time. A silent
# publish is the worst possible failure here -- the agent just sits there and
# the whole sweep scores zero for a reason nothing reports.
TEXT="$(cat)"
python3 - "$TEXT" > /tmp/say_payload.py <<'PY'
import json, sys
msg = json.dumps({"text": sys.argv[1]})
print("import subprocess, json")
print(f"subprocess.run(['ros2','topic','pub','--once','/brain/chat_in','std_msgs/String',"
      f"{json.dumps(json.dumps({'data': msg}))}], timeout=40)")
PY
docker cp /tmp/say_payload.py innate-dev:/tmp/say_payload.py >/dev/null
# RMW_IMPLEMENTATION: exec shells do not inherit it, so without this the
# publish goes to a DDS graph nobody is on -- it succeeds, and the brain never
# hears the brief. Same silent no-op the rosbridge path had.
docker exec -e RMW_IMPLEMENTATION=rmw_zenoh_cpp innate-dev bash -lc 'source /opt/ros/humble/setup.bash; source /root/innate-os/ros2_ws/install/setup.bash; python3 /tmp/say_payload.py' >/dev/null 2>&1
