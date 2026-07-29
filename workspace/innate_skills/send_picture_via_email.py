#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Send Picture Via Email Skill - send an email with an attached picture.
"""

import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from innate import MainImage, Skill, SkillReturn

DEFAULT_RECIPIENT = "axel@innate.bot"
# Email server configuration (same as send_email)
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "axel@innate.bot"
SENDER_PASSWORD = ""  # Use app password for Gmail


class SendPictureViaEmail(Skill):
    """Use to send an email with the latest view from the robot eyes.
    Provide a subject and a message body. The view will be automatically
    attached."""

    image: MainImage

    def execute(self, subject: str, message: str, recipient: str | None = None) -> SkillReturn:
        if not recipient:  # Checks for None or empty string
            recipient = DEFAULT_RECIPIENT

        self.logger.info(f"\\033[96m[BrainClient] Sending email with picture to {recipient}\\033[0m")

        try:
            image_data = self.image.jpeg

            # Create message
            msg = MIMEMultipart()
            msg["From"] = SENDER_EMAIL
            msg["To"] = recipient
            msg["Subject"] = subject

            # Attach the text message
            msg.attach(MIMEText(message, "plain"))

            # Attach the image
            image = MIMEImage(image_data, name="robot_capture.jpg")
            msg.attach(image)

            # Connect to server and send
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
            server.quit()

            self.logger.info(f"\\033[92m[BrainClient] Email with picture sent to {recipient}\\033[0m")
            return f"Email with picture sent to {recipient}"

        except Exception as e:
            self.logger.error(f"[SendPictureViaEmail] Failed to send email: {str(e)}")
            self.fail(f"Failed to send email: {str(e)}")
