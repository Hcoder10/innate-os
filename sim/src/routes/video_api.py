import asyncio
import json
import os
import time
import uuid

import cv2
import websockets
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from src.agent.types import ActiveSkillsCmd, BrainActiveCmd, DirectiveCmd, RefreshAgentsCmd

router = APIRouter()
AGENT_REFRESH_TIMEOUT_SECONDS = 5.0
AGENT_REFRESH_POLL_SECONDS = 0.05
ROSBRIDGE_SERVICE_TIMEOUT_SECONDS = 10.0
ROSBRIDGE_ACTION_TIMEOUT_SECONDS = 300.0
ROSBRIDGE_PUBLISH_LINGER_SECONDS = 0.12
EXECUTE_SKILL_ACTION = "/execute_skill"
EXECUTE_SKILL_ACTION_TYPE = "brain_messages/action/ExecuteSkill"


# Create a model for the reset robot request
class ResetRobotRequest(BaseModel):
    memory_state: str | None = None
    position: list[float] | None = None
    orientation: list[float] | None = None


# Create a model for the brain activation request
class SetBrainActiveRequest(BaseModel):
    active: bool


class SetActiveSkillsRequest(BaseModel):
    agent_id: str | None = None
    skills: list[str]


class ExecuteSkillRequest(BaseModel):
    skill_type: str
    inputs: str = "{}"


class ManualSkillEventRequest(BaseModel):
    status: str
    skill_id: str
    skill_name: str
    primitive_id: str
    inputs: str | None = None
    reason: str | None = None
    source: str | None = None


def rosbridge_uri() -> str:
    return os.getenv("ROSBRIDGE_URI", "ws://localhost:9090")


def make_rosbridge_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


def is_terminal_action_status(status) -> bool:
    return status in (4, 5, 6)


def normalize_action_result(skill_type: str, status, values: dict | None) -> dict:
    result = dict(values or {})
    if status == 5:
        result.setdefault("success", False)
        result.setdefault("skill_type", skill_type)
        result.setdefault("success_type", "cancelled")
        result.setdefault("message", "Action was cancelled")
    elif status == 6:
        result.setdefault("success", False)
        result.setdefault("skill_type", skill_type)
        result.setdefault("success_type", "failure")
        result.setdefault("message", "Action was aborted")
    return result


async def publish_rosbridge_topic(topic: str, msg: dict) -> None:
    async with websockets.connect(rosbridge_uri()) as ws:
        await ws.send(json.dumps({"op": "publish", "topic": topic, "msg": msg}))
        await asyncio.sleep(ROSBRIDGE_PUBLISH_LINGER_SECONDS)


async def call_rosbridge_service(service: str, args: dict) -> dict:
    call_id = make_rosbridge_id("svc")
    deadline = time.monotonic() + ROSBRIDGE_SERVICE_TIMEOUT_SECONDS
    async with websockets.connect(rosbridge_uri()) as ws:
        await ws.send(json.dumps({"op": "call_service", "id": call_id, "service": service, "args": args}))
        while time.monotonic() < deadline:
            timeout = max(0.0, deadline - time.monotonic())
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            message = json.loads(raw)
            if message.get("op") != "service_response" or message.get("id") != call_id:
                continue
            if message.get("result") is False:
                raise RuntimeError(f"Service {service} returned result=false")
            return message.get("values") or {}
    raise TimeoutError(f"Service {service} timed out")


async def execute_rosbridge_action(skill_type: str, inputs: str) -> dict:
    call_id = make_rosbridge_id("action")
    goal_id = str(uuid.uuid4())
    deadline = time.monotonic() + ROSBRIDGE_ACTION_TIMEOUT_SECONDS
    async with websockets.connect(rosbridge_uri()) as ws:
        await ws.send(
            json.dumps(
                {
                    "op": "send_action_goal",
                    "id": call_id,
                    "action": EXECUTE_SKILL_ACTION,
                    "action_type": EXECUTE_SKILL_ACTION_TYPE,
                    "args": {"skill_type": skill_type, "inputs": inputs},
                    "feedback": True,
                    "goal_id": goal_id,
                }
            )
        )
        while time.monotonic() < deadline:
            timeout = max(0.0, deadline - time.monotonic())
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            message = json.loads(raw)
            if message.get("op") != "action_result" or message.get("id") != call_id:
                continue
            status = message.get("status")
            if not is_terminal_action_status(status):
                continue
            return normalize_action_result(skill_type, status, message.get("values"))
    raise TimeoutError(f"Action {EXECUTE_SKILL_ACTION} timed out")


def available_agents_payload(shared_queues, error: str | None = None) -> dict:
    (
        agents,
        skills,
        current_agent_id,
        active_skill_ids,
        brain_active,
    ) = shared_queues.get_available_agents()
    brain_backend_status = shared_queues.get_brain_backend_status()

    agents_data = [
        {
            "id": agent.id,
            "display_name": agent.display_name,
            "display_icon": agent.display_icon,
            "prompt": agent.prompt,
            "skills": agent.skills,
        }
        for agent in agents
    ]
    skills_data = [
        {
            "id": skill.id,
            "name": skill.name,
            "type": skill.type,
            "guidelines": skill.guidelines,
            "guidelines_when_running": skill.guidelines_when_running,
            "inputs": skill.inputs,
            "in_training": skill.in_training,
            "episode_count": skill.episode_count,
            "directory": skill.directory,
        }
        for skill in skills
    ]

    payload = {
        "agents": agents_data,
        "skills": skills_data,
        "current_agent_id": current_agent_id,
        "active_skill_ids": active_skill_ids,
        "brain_active": brain_active,
        "brain_backend_status": brain_backend_status,
    }
    if error:
        payload["error"] = error
    return payload


async def wait_for_available_agents_update(shared_queues, previous_updated_at: float):
    deadline = time.time() + AGENT_REFRESH_TIMEOUT_SECONDS
    while time.time() < deadline:
        await asyncio.sleep(AGENT_REFRESH_POLL_SECONDS)
        if shared_queues.get_available_agents_updated_at() > previous_updated_at:
            return True
    return False


def mjpeg_generator(shared_queues, camera_name="first_person"):
    """
    Continuously yields JPEG frames from the simulation.
    Uses the shared_queues (attached on app.state) for the latest frames.
    """
    while True:
        if shared_queues is None:
            time.sleep(0.1)
            continue

        frame = shared_queues.latest_frames.get(camera_name)
        if frame is None:
            time.sleep(0.01)
            continue

        shared_queues.latest_frames[camera_name] = None

        success, encoded_image = cv2.imencode(".jpg", frame)
        if not success:
            continue

        yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + encoded_image.tobytes() + b"\r\n")


@router.get("/video_feeds_ready")
def video_feeds_ready(request: Request):
    """
    Simple endpoint to check if the video feeds are ready.
    Just checks if shared_queues exists, which indicates the simulation is running.
    """
    shared_queues = request.app.state.SHARED_QUEUES

    # Simply check if shared_queues exists
    is_ready = shared_queues is not None

    return JSONResponse(
        {
            "ready": is_ready,
            "message": ("Simulation is running" if is_ready else "Simulation not initialized"),
        }
    )


@router.get("/stack_metrics")
def stack_metrics(request: Request):
    """Return lightweight simulator/runtime metrics for local stack dashboards."""
    shared_queues = request.app.state.SHARED_QUEUES
    if shared_queues is None:
        return JSONResponse(
            {
                "ready": False,
                "queue_sizes": {},
                "fps_by_camera": {},
                "latest_frame_age_by_camera": {},
                "brain_backend_status": {
                    "state": "sim_not_initialized",
                    "connected": False,
                    "message": "Simulation not initialized",
                    "updated_at": time.time(),
                    "uri": None,
                    "hosted": None,
                },
            }
        )

    metrics = shared_queues.get_runtime_metrics()
    return JSONResponse(
        {
            "ready": True,
            **metrics,
            "brain_backend_status": shared_queues.get_brain_backend_status(),
        }
    )


@router.get("/available_agents")
def available_agents(request: Request):
    """Return the cached available directives list without asking ROS to refresh."""
    shared_queues = request.app.state.SHARED_QUEUES

    if shared_queues is None:
        return JSONResponse(
            {
                "agents": [],
                "skills": [],
                "current_agent_id": None,
                "active_skill_ids": [],
                "error": "Simulation not initialized",
                "brain_backend_status": {
                    "state": "sim_not_initialized",
                    "connected": False,
                    "message": "Simulation not initialized",
                    "updated_at": time.time(),
                    "uri": None,
                    "hosted": None,
                },
            },
            status_code=200,
        )

    return JSONResponse(available_agents_payload(shared_queues))


@router.get("/video_feed", include_in_schema=False)
def video_feed(request: Request):
    """
    Streaming endpoint which returns the primary camera feed.
    Retrieves the shared queues from the application's state.
    """
    shared_queues = request.app.state.SHARED_QUEUES
    return StreamingResponse(
        mjpeg_generator(shared_queues, "first_person"),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/video_feed_chase", include_in_schema=False)
def video_feed_chase(request: Request):
    """
    Streaming endpoint which returns the chase camera feed.
    """
    shared_queues = request.app.state.SHARED_QUEUES
    return StreamingResponse(
        mjpeg_generator(shared_queues, "chase"),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/get_robot_position")
def get_robot_position(request: Request):
    """
    Returns the current 3D position (x, y, z) of the robot.
    Uses the SharedQueues' direct robot position tracking.

    Returns:
        JSON response with position [x, y, z] and timestamp
    """
    shared_queues = request.app.state.SHARED_QUEUES

    # Check if we have valid shared_queues
    if shared_queues is None:
        return JSONResponse(
            {
                "position": [0.0, 0.0, 0.0],  # Default position
                "timestamp": time.time(),
                "error": "Simulation not initialized",
            },
            status_code=200,  # Still return 200 to avoid breaking clients
        )

    # Retrieve position and timestamp directly from shared queues
    position, timestamp = shared_queues.get_robot_position()

    # Convert any NumPy types to native Python types
    position = [float(p) for p in position]  # Convert to native Python floats

    return JSONResponse(
        {
            "position": position,
            "timestamp": float(timestamp),  # Ensure timestamp is also a Python float
        }
    )


@router.post("/set_directive")
async def set_directive(request: Request, directive: dict):
    """
    Enqueues a directive command to update the robot's behavior.
    Retrieves the shared queues from the application's state.
    """
    shared_queues = request.app.state.SHARED_QUEUES
    if shared_queues is not None:
        try:
            shared_queues.sim_to_agent.put_nowait(DirectiveCmd(directive=directive["text"]))
        except Exception:
            return {"status": "queue_full"}
        return {"status": "directive_enqueued"}
    else:
        return {"status": "no_shared_queues"}


@router.post("/set_active_skills")
async def set_active_skills(request: Request, skills_request: SetActiveSkillsRequest):
    """
    Enqueues an active-skills command for the current robot directive.
    """
    shared_queues = request.app.state.SHARED_QUEUES
    if shared_queues is not None:
        try:
            shared_queues.sim_to_agent.put_nowait(
                ActiveSkillsCmd(
                    agent_id=skills_request.agent_id,
                    skills=skills_request.skills,
                )
            )
        except Exception:
            return {"status": "queue_full"}
        return {"status": "active_skills_enqueued"}
    else:
        return {"status": "no_shared_queues"}


@router.post("/set_brain_active")
async def set_brain_active(request: Request, brain_request: SetBrainActiveRequest):
    """
    Activates or deactivates the brain by sending a command to the agent.
    """
    shared_queues = request.app.state.SHARED_QUEUES
    if shared_queues is not None:
        try:
            shared_queues.sim_to_agent.put_nowait(BrainActiveCmd(active=brain_request.active))
            shared_queues.set_brain_active(brain_request.active)
            return {"status": "brain_command_enqueued"}
        except Exception:
            return {"status": "queue_full"}
    else:
        return {"status": "no_shared_queues"}


@router.post("/manual_skill_event")
async def manual_skill_event(request: Request, event: ManualSkillEventRequest):
    """Publish a manual skill lifecycle event through the simulator host."""
    shared_queues = request.app.state.SHARED_QUEUES
    if shared_queues is None:
        return JSONResponse(
            {"status": "no_shared_queues", "error": "Simulation not initialized"},
            status_code=503,
        )

    try:
        payload = event.dict()
        payload["timestamp"] = time.time()
        await publish_rosbridge_topic("/brain/manual_skill_event", {"data": json.dumps(payload)})
        return {"status": "manual_skill_event_published"}
    except Exception as exc:
        return JSONResponse(
            {
                "status": "rosbridge_unavailable",
                "error": f"Failed to publish manual skill event: {exc}",
            },
            status_code=502,
        )


@router.post("/execute_skill")
async def execute_skill(request: Request, skill_request: ExecuteSkillRequest):
    """Execute a skill via the simulator host's local ROSBridge connection."""
    shared_queues = request.app.state.SHARED_QUEUES
    if shared_queues is None:
        return JSONResponse(
            {"success": False, "success_type": "failure", "message": "Simulation not initialized"},
            status_code=503,
        )

    try:
        result = await execute_rosbridge_action(skill_request.skill_type, skill_request.inputs)
        return JSONResponse(result)
    except TimeoutError as exc:
        return JSONResponse(
            {"success": False, "success_type": "failure", "message": str(exc)},
            status_code=504,
        )
    except Exception as exc:
        return JSONResponse(
            {
                "success": False,
                "success_type": "failure",
                "message": f"Failed to execute skill through ROSBridge: {exc}",
            },
            status_code=502,
        )


@router.post("/cancel_skill_execution")
async def cancel_skill_execution(request: Request):
    """Cancel currently running skill actions through the simulator host."""
    shared_queues = request.app.state.SHARED_QUEUES
    if shared_queues is None:
        return JSONResponse(
            {"status": "no_shared_queues", "error": "Simulation not initialized"},
            status_code=503,
        )

    try:
        values = await call_rosbridge_service(
            "/execute_skill/_action/cancel_goal",
            {
                "goal_info": {
                    "goal_id": {"uuid": [0] * 16},
                    "stamp": {"sec": 0, "nanosec": 0},
                }
            },
        )
        return {"status": "skill_cancel_requested", "values": values}
    except Exception as exc:
        return JSONResponse(
            {
                "status": "rosbridge_unavailable",
                "error": f"Failed to cancel skill execution: {exc}",
            },
            status_code=502,
        )


@router.post("/reload_available_agents")
async def reload_available_agents(request: Request):
    """Ask the robot brain to refresh its available directives list."""
    shared_queues = request.app.state.SHARED_QUEUES
    if shared_queues is None:
        return JSONResponse(
            {"status": "no_shared_queues", "error": "Simulation not initialized"},
            status_code=503,
        )

    try:
        previous_updated_at = shared_queues.get_available_agents_updated_at()
        shared_queues.sim_to_agent.put_nowait(RefreshAgentsCmd())
        refreshed = await wait_for_available_agents_update(shared_queues, previous_updated_at)
        payload = available_agents_payload(shared_queues)
        payload["status"] = "agent_refresh_completed" if refreshed else "agent_refresh_pending"
        payload["refresh_pending"] = not refreshed
        if not refreshed:
            payload["error"] = "Agent refresh was queued, but no updated directives arrived yet."
        return JSONResponse(payload)
    except Exception:
        return JSONResponse(
            {"status": "queue_full", "error": "Could not enqueue agent refresh"},
            status_code=503,
        )
