"""Unix-socket bridge hosted inside SkillsActionServer for low-latency CLI calls."""

from __future__ import annotations

import json
import os
import select
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

DEFAULT_SOCKET_PATH = "/tmp/innate_skill_cli.sock"


class SkillCliTask:
    """Execution task submitted from a socket handler to the ROS executor thread."""

    def __init__(self, skill_type: str, inputs_json: str, feedback_callback: Callable):
        self.skill_type = skill_type
        self.inputs_json = inputs_json
        self.feedback_callback = feedback_callback
        self.started_event = threading.Event()
        self.done_event = threading.Event()
        self.cancel_event = threading.Event()
        self.result = None
        self.error_message = ""
        self._cancel_handler = None

    def set_cancel_handler(self, cancel_handler: Callable[[], None]) -> None:
        self._cancel_handler = cancel_handler

    def mark_started(self) -> None:
        self.started_event.set()

    def set_result(self, result) -> None:
        if not self.done_event.is_set():
            self.result = result
            self.done_event.set()

    def set_error(self, message: str) -> None:
        if not self.done_event.is_set():
            self.error_message = message
            self.done_event.set()

    def cancel(self) -> None:
        self.cancel_event.set()
        if self._cancel_handler is not None:
            self._cancel_handler()


class SkillCliGoalHandle:
    """Small subset of rclpy's server goal handle used by SkillsActionServer."""

    def __init__(self, task: SkillCliTask):
        self._task = task
        self.request = SimpleNamespace(skill_type=task.skill_type, inputs=task.inputs_json)
        self.status = "accepted"

    @property
    def is_cancel_requested(self) -> bool:
        return self._task.cancel_event.is_set()

    def publish_feedback(self, feedback_msg) -> None:
        self._task.feedback_callback(feedback_msg)

    def succeed(self) -> None:
        self.status = "succeeded"

    def abort(self) -> None:
        self.status = "aborted"

    def canceled(self) -> None:
        self.status = "canceled"


class SkillCliBridge:
    def __init__(self, logger, submit_skill: Callable[[SkillCliTask], None]):
        self._logger = logger
        self._submit_skill = submit_skill
        self._socket_path = Path(os.environ.get("INNATE_SKILL_SOCKET", DEFAULT_SOCKET_PATH))
        self._stop_event = threading.Event()
        self._server_socket = None
        self._server_thread = threading.Thread(target=self._serve, daemon=True)
        self._server_thread.start()

    def _serve(self) -> None:
        try:
            self._socket_path.unlink(missing_ok=True)
            self._socket_path.parent.mkdir(parents=True, exist_ok=True)

            server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server_socket.bind(str(self._socket_path))
            os.chmod(self._socket_path, 0o600)
            server_socket.listen(8)
            server_socket.settimeout(0.2)
            self._server_socket = server_socket
            self._logger.info(f"Skill CLI socket listening at {self._socket_path}")
        except OSError as exc:
            self._logger.error(f"Failed to start skill CLI socket {self._socket_path}: {exc}")
            return

        while not self._stop_event.is_set():
            try:
                conn, _addr = server_socket.accept()
            except TimeoutError:
                continue
            except OSError:
                break

            threading.Thread(target=self._handle_connection, args=(conn,), daemon=True).start()

    def _handle_connection(self, conn: socket.socket) -> None:
        with conn:
            request = self._read_request(conn)
            if request is None:
                return

            if request.get("action") != "run":
                self._send_event(conn, "error", message="Unsupported skill CLI action")
                return

            skill_type = str(request.get("skill_type") or "").strip()
            if not skill_type:
                self._send_event(conn, "error", message="Missing skill_type")
                return

            try:
                inputs_json = self._normalize_inputs(request.get("inputs", "{}"))
                timeout = self._optional_float(request.get("timeout"))
                start_timeout = self._optional_float(request.get("server_timeout")) or 5.0
            except ValueError as exc:
                self._send_event(conn, "error", message=str(exc))
                return

            self._run_skill(conn, skill_type, inputs_json, timeout, start_timeout)

    def _read_request(self, conn: socket.socket):
        conn.settimeout(1.0)
        data = b""
        try:
            while b"\n" not in data and len(data) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    return None
                data += chunk
        except OSError:
            return None

        line = data.split(b"\n", 1)[0]
        try:
            request = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_event(conn, "error", message="Invalid JSON request")
            return None
        return request if isinstance(request, dict) else None

    def _normalize_inputs(self, raw_inputs) -> str:
        if isinstance(raw_inputs, dict):
            return json.dumps(raw_inputs, separators=(",", ":"))
        if not isinstance(raw_inputs, str):
            raise ValueError("inputs must be a JSON object")
        try:
            parsed = json.loads(raw_inputs)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid inputs JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("inputs must be a JSON object")
        return json.dumps(parsed, separators=(",", ":"))

    def _optional_float(self, value):
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Expected a number, got {value!r}") from exc

    def _run_skill(
        self, conn: socket.socket, skill_type: str, inputs_json: str, timeout: float | None, start_timeout: float
    ) -> None:
        task = SkillCliTask(
            skill_type=skill_type,
            inputs_json=inputs_json,
            feedback_callback=lambda feedback_msg: self._send_feedback(conn, feedback_msg),
        )
        self._submit_skill(task)

        if not self._wait_for_start_or_disconnect(conn, task, start_timeout):
            return

        wait_state = self._wait_for_result_or_disconnect(conn, task, timeout)
        if wait_state == "disconnected":
            task.cancel()
            return
        if wait_state == "timeout":
            task.cancel()
            self._send_event(conn, "result", success=False, message="Timed out waiting for skill result")
            return

        if task.error_message:
            self._send_event(conn, "error", message=task.error_message)
            return

        result = task.result
        if result is None:
            self._send_event(conn, "error", message="Skill execution returned no result")
            return

        self._send_event(
            conn,
            "result",
            success=bool(result.success),
            message=result.message,
            skill_type=result.skill_type,
            success_type=result.success_type,
        )

    def _wait_for_start_or_disconnect(self, conn: socket.socket, task: SkillCliTask, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not task.started_event.is_set() and not task.done_event.is_set():
            if not self._client_connected(conn):
                task.cancel()
                return False
            if time.monotonic() >= deadline:
                task.cancel()
                self._send_event(conn, "error", message="Timed out waiting for skill execution to start")
                return False
            time.sleep(0.001)
        return True

    def _wait_for_result_or_disconnect(self, conn: socket.socket, task: SkillCliTask, timeout: float | None) -> str:
        deadline = time.monotonic() + timeout if timeout is not None else None
        while not task.done_event.is_set():
            if not self._client_connected(conn):
                return "disconnected"
            if deadline is not None and time.monotonic() >= deadline:
                return "timeout"
            time.sleep(0.001)
        return "done"

    def _client_connected(self, conn: socket.socket) -> bool:
        try:
            readable, _, _ = select.select([conn], [], [], 0)
            if not readable:
                return True
            return bool(conn.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT))
        except BlockingIOError:
            return True
        except OSError:
            return False

    def _send_feedback(self, conn: socket.socket, feedback_msg) -> None:
        text = getattr(feedback_msg, "feedback", "")
        if text:
            self._send_event(conn, "feedback", text=text)

    def _send_event(self, conn: socket.socket, event: str, **payload) -> bool:
        payload["event"] = event
        try:
            conn.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
            return True
        except OSError:
            return False

    def stop(self) -> None:
        self._stop_event.set()
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None
        self._server_thread.join(timeout=1.0)
        try:
            self._socket_path.unlink(missing_ok=True)
        except OSError:
            pass
