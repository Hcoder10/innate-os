import time

import cv2
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

router = APIRouter()


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


@router.get("/video_feed_arm", include_in_schema=False)
def video_feed_arm(request: Request):
    """
    Streaming endpoint which returns the arm wrist camera feed.
    """
    shared_queues = request.app.state.SHARED_QUEUES
    return StreamingResponse(
        mjpeg_generator(shared_queues, "arm_wrist"),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )
