#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import base64
import time

from brain_client.skills.types import RobotState, RobotStateType, Skill, SkillResult


class SendPictureViaEmail(Skill):
    """
    Primitive for sending an email with an attached picture.

    Like SendEmail, this is a simplified version that logs the notification
    (with the captured view) rather than actually sending over SMTP. In a
    production environment you would configure proper mail credentials.
    """

    # Declare required robot state using descriptor
    image = RobotState(RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64)

    def __init__(self, logger):
        super().__init__(logger)
        self.default_recipient = "axel@innate.bot"

    @property
    def name(self):
        return "send_picture_via_email"

    def guidelines(self):
        return (
            "Use to send an email with the latest view from the robot eyes. "
            "Provide a subject and a message body. "
            "The view will be automatically attached."
        )

    def execute(self, subject: str, message: str, recipient: str = None):
        """
        Sends an email with the last captured image attached.

        Args:
            subject (str): Email subject line.
            message (str): Email body content.
            recipient (str, optional): Email recipient. Defaults to default_recipient.

        Returns:
            tuple: (result_message, result_status)
        """
        if not recipient:  # Checks for None or empty string
            recipient = self.default_recipient

        # The main-camera image is filled in asynchronously once the camera node
        # warms up, so wait briefly for the first frame instead of failing fast.
        image_b64 = self._await_image(timeout=5.0)
        if not image_b64:
            self.logger.error("[SendPictureViaEmail] No image available to send.")
            return "No image available to send", SkillResult.FAILURE

        try:
            image_data = base64.b64decode(image_b64)
        except Exception as e:
            self.logger.error(f"[SendPictureViaEmail] Invalid image data: {e}")
            return f"Invalid image data: {e}", SkillResult.FAILURE

        self.logger.info(
            f"\033[96m[BrainClient] Sending email with picture to {recipient}\033[0m\n"
            f"Subject: {subject}\n"
            f"Message: {message}\n"
            f"Attachment: robot_capture.jpg ({len(image_data)} bytes)"
        )
        self.logger.info(f"\033[92m[BrainClient] Email with picture sent to {recipient}\033[0m")
        return f"Email with picture sent to {recipient}", SkillResult.SUCCESS

    def _await_image(self, timeout: float = 5.0):
        """Wait up to ``timeout`` seconds for the main-camera image to populate."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.image:
                return self.image
            time.sleep(0.1)
        return self.image

    def cancel(self):
        """
        Cancel the email sending operation (typically quick, so not much to do).
        """
        self.logger.info("\033[91m[BrainClient] Email sending cannot be effectively canceled once started.\033[0m")
        return "Email sending is an atomic operation and cannot be effectively canceled."
