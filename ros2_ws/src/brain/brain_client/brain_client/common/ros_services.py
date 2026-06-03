"""Helpers for calling ROS services synchronously from brain_client nodes.

Each helper spins the given node while waiting and logs the outcome, so callers
get a one-line service call instead of repeating the wait/spin/branch ladder.
``node`` is the node to spin (which may differ from the node that owns
``logger``), and ``label`` is used to identify the call in log messages.
"""

import rclpy
from std_srvs.srv import Trigger


def call_service_sync(node, logger, client, request, label, timeout_sec, wait_timeout_sec=5.0):
    """Call a service and block for the response, logging the outcome.

    Returns the response (which may itself carry ``success == False``), or None if
    the service was unavailable or the call timed out. Expects a response with
    ``success`` and ``message`` fields.
    """
    if not client.wait_for_service(timeout_sec=wait_timeout_sec):
        logger.warn(f"{label} service not available")
        return None

    future = client.call_async(request)
    rclpy.spin_until_future_complete(node, future, timeout_sec=timeout_sec)
    if not future.done():
        logger.warn(f"{label} timed out")
        return None

    result = future.result()
    if result.success:
        logger.info(f"{label}: {result.message}")
    else:
        logger.warn(f"{label} failed: {result.message}")
    return result


def await_motion_result(node, logger, future, label, timeout_sec):
    """Block on an in-flight motion-service future; return True iff it succeeded."""
    rclpy.spin_until_future_complete(node, future, timeout_sec=timeout_sec)
    result = future.result()
    if result is None:
        logger.error(f"{label} call timed out")
        return False
    if not result.success:
        logger.error(f"{label} returned failure")
        return False
    return True


def call_trigger(node, logger, client, label, success_msg, timeout_sec=2.0):
    """Call a std_srvs/Trigger service, log the outcome, and return whether it succeeded."""
    if not client.service_is_ready():
        logger.error(f"{label} service not ready")
        return False

    try:
        future = client.call_async(Trigger.Request())
        rclpy.spin_until_future_complete(node, future, timeout_sec=timeout_sec)
        result = future.result()
        if result is None:
            logger.error(f"{label} service call timed out")
            return False
        if not result.success:
            logger.error(f"{label} failed: {result.message}")
            return False
        logger.info(success_msg)
        return True
    except Exception as e:
        logger.error(f"Exception calling {label}: {e}")
        return False
