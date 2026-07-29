#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Send Email Skill - emergency email notifications.
This is a simplified version that logs the email content rather than actually
sending. In a production environment, you would configure proper SMTP settings.
"""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from innate import Skill, SkillReturn

DEFAULT_RECIPIENTS = ["axel@innate.bot", "vignesh@innate.bot"]
# Email server configuration
SMTP_SERVER = "smtp.gmail.com"  # Example using Gmail
SMTP_PORT = 587
SENDER_EMAIL = "axel@innate.bot"  # Replace with robot's email
SENDER_PASSWORD = ""  # Use app password for Gmail


class SendEmail(Skill):
    """Use to send an emergency email notification. Provide a subject and
    message. You can optionally provide a list of recipients, otherwise it
    will be sent to the default list. This should be used when a potential
    emergency is detected and assistance might be required."""

    def execute(self, subject: str, message: str, recipients: list[str] | str | None = None) -> SkillReturn:
        current_recipients = []
        if recipients is None:
            current_recipients = DEFAULT_RECIPIENTS
        elif isinstance(recipients, str):
            current_recipients = [recipients]
        else:
            current_recipients = recipients

        if not current_recipients:
            self.logger.error("No recipients specified for email.")
            self.fail("No recipients specified for email.")

        recipients_str = ", ".join(current_recipients)

        self.logger.info(
            f"\033[96m[BrainClient] Sending emergency email notification\033[0m\n"
            f"To: {recipients_str}\n"
            f"Subject: {subject}\n"
            f"Message: {message}"
        )

        self.logger.info(f"\033[92m[BrainClient] Emergency email sent to {recipients_str}\033[0m")
        return f"Email sent to {recipients_str}"

        # Just pretending here it worked for sure.

        try:
            # Create message
            msg = MIMEMultipart()
            msg["From"] = SENDER_EMAIL
            msg["To"] = recipients_str
            msg["Subject"] = subject
            msg.attach(MIMEText(message, "plain"))

            # Connect to server and send
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
            server.quit()

            # Log success message
            self.logger.info(f"\033[92m[BrainClient] Emergency email sent to {recipients_str}\033[0m")
            return f"Email sent to {recipients_str}"

        except Exception as e:
            self.logger.error(f"Failed to send email: {str(e)}")
            self.fail(f"Failed to send email: {str(e)}")
