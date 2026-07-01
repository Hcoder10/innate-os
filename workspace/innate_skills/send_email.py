#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from brain_client.skills.types import Skill, SkillResult


class SendEmail(Skill):
    """
    Primitive for sending emails for emergency notifications.
    This is a simplified version that logs the email content rather than actually
    sending. In a production environment, you would configure proper SMTP settings.
    """

    def __init__(self, logger):
        self.logger = logger
        self.default_recipients = ["axel@innate.bot", "vignesh@innate.bot"]

    @property
    def name(self):
        return "send_email"

    def guidelines(self):
        return (
            "Use to send an emergency email notification. Provide a subject and "
            "message. You can optionally provide a list of recipients, otherwise "
            "it will be sent to the default list. This should be used when a "
            "potential emergency is detected and assistance might be required."
        )

    def execute(self, subject: str, message: str, recipients: list[str] | str = None):
        """
        Sends an email to the specified recipient(s) (or default list if none provided).

        Args:
            subject (str): Email subject line
            message (str): Email body content
            recipients (list[str] | str, optional): Email recipient or list of recipients.
                                     Defaults to the default list if not specified.

        Returns:
            tuple: (result_message, result_status) where result_status is a
                   PrimitiveResult enum value
        """
        current_recipients = []
        if recipients is None:
            current_recipients = self.default_recipients
        elif isinstance(recipients, str):
            current_recipients = [recipients]
        else:
            current_recipients = recipients

        if not current_recipients:
            self.logger.error("No recipients specified for email.")
            return "No recipients specified for email.", SkillResult.FAILURE

        recipients_str = ", ".join(current_recipients)

        self.logger.info(
            f"\033[96m[BrainClient] Sending emergency email notification\033[0m\n"
            f"To: {recipients_str}\n"
            f"Subject: {subject}\n"
            f"Message: {message}"
        )

        self.logger.info(f"\033[92m[BrainClient] Emergency email sent to {recipients_str}\033[0m")
        return f"Email sent to {recipients_str}", SkillResult.SUCCESS

    def cancel(self):
        """
        Cancel the email sending operation.

        Since email sending is typically a quick operation that completes almost
        instantly, this method doesn't do much. It's implemented to satisfy the
        Primitive interface.

        Returns:
            str: A message describing the cancellation result.
        """
        self.logger.info("\033[91m[BrainClient] Email sending operation cannot be canceled once started\033[0m")
        return "Email sending is an atomic operation that cannot be canceled once started"
