# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import Skill, SkillReturn

DEFAULT_RECIPIENTS = ["axel@innate.bot", "vignesh@innate.bot"]


class SendEmail(Skill):
    """Use to send an emergency email notification. Provide a subject and
    message. You can optionally provide a list of recipients, otherwise it
    will be sent to the default list. This should be used when a potential
    emergency is detected and assistance might be required."""

    def execute(self, subject: str, message: str, recipients: list[str] | str | None = None) -> SkillReturn:
        if recipients is None:
            recipients = DEFAULT_RECIPIENTS
        elif isinstance(recipients, str):
            recipients = [recipients]
        if not recipients:
            self.fail("No recipients specified for email.")

        # Demo stub: logs the email instead of talking to an SMTP server.
        recipients_str = ", ".join(recipients)
        self.logger.info(f"[SendEmail] To: {recipients_str}\nSubject: {subject}\nMessage: {message}")
        return f"Email sent to {recipients_str}"
