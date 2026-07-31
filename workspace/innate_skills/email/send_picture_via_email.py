# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from innate import MainImage, Skill, SkillReturn

DEFAULT_RECIPIENT = "axel@innate.bot"
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "axel@innate.bot"
SENDER_PASSWORD = ""  # Gmail app password


class SendPictureViaEmail(Skill):
    """Use to send an email with the latest view from the robot eyes.
    Provide a subject and a message body. The view will be automatically
    attached."""

    image: MainImage

    def execute(self, subject: str, message: str, recipient: str | None = None) -> SkillReturn:
        recipient = recipient or DEFAULT_RECIPIENT
        try:
            msg = MIMEMultipart()
            msg["From"] = SENDER_EMAIL
            msg["To"] = recipient
            msg["Subject"] = subject
            msg.attach(MIMEText(message, "plain"))
            msg.attach(MIMEImage(self.image.jpeg, name="robot_capture.jpg"))

            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
            server.quit()
        except Exception as e:
            self.fail(f"Failed to send email: {e}")
        return f"Email with picture sent to {recipient}"
