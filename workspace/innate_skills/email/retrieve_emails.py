# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import email
import imaplib
from email.header import decode_header

from innate import Skill, SkillFailed, SkillReturn

IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993
EMAIL_ADDRESS = ""  # configure account
EMAIL_PASSWORD = ""  # configure app password
MAX_CONTENT_CHARS = 500


def _decode_header_value(value):
    if value is None:
        return ""
    decoded = ""
    for part, encoding in decode_header(value):
        if isinstance(part, bytes):
            try:
                decoded += part.decode(encoding or "utf-8")
            except (UnicodeDecodeError, LookupError):
                decoded += part.decode("utf-8", errors="ignore")
        else:
            decoded += str(part)
    return decoded


def _extract_content(msg):
    if not msg.is_multipart():
        body = msg.get_payload(decode=True)
        return (body.decode("utf-8", errors="ignore") if body else "").strip()
    plain, html = "", ""
    for part in msg.walk():
        if "attachment" in str(part.get("Content-Disposition")):
            continue
        body = part.get_payload(decode=True)
        if not body:
            continue
        if part.get_content_type() == "text/plain":
            plain += body.decode("utf-8", errors="ignore")
        elif part.get_content_type() == "text/html" and not plain:
            html += body.decode("utf-8", errors="ignore")
    return (plain or html).strip()


class RetrieveEmails(Skill):
    """Use to retrieve recent emails from the configured email account.
    Provide the number of emails to retrieve (default is 5). Returns email
    subjects and content. This should be used when you need to check for
    recent messages or respond to incoming communications."""

    def execute(self, count: int = 5) -> SkillReturn:
        count = min(max(1, count), 20)
        try:
            return self._retrieve(count)
        except SkillFailed:
            raise
        except imaplib.IMAP4.error as e:
            self.fail(f"IMAP error: {e}")
        except Exception as e:
            self.fail(f"Failed to retrieve emails: {e}")

    def _retrieve(self, count: int) -> str:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        mail.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        mail.select("INBOX")

        status, messages = mail.search(None, "ALL")
        if status != "OK":
            mail.logout()
            self.fail("Failed to search emails")

        email_ids = messages[0].split()
        if not email_ids:
            mail.logout()
            return "No emails found in inbox"

        emails = []
        for email_id in reversed(email_ids[-count:]):
            status, msg_data = mail.fetch(email_id, "(RFC822)")
            payload = msg_data[0] if status == "OK" else None
            if not isinstance(payload, tuple):
                self.logger.warning(f"Failed to fetch email {email_id}")
                continue
            msg = email.message_from_bytes(payload[1])
            content = _extract_content(msg)
            if len(content) > MAX_CONTENT_CHARS:
                content = content[:MAX_CONTENT_CHARS] + "... [truncated]"
            emails.append(
                {
                    "subject": _decode_header_value(msg.get("Subject", "No Subject")),
                    "from": _decode_header_value(msg.get("From", "Unknown Sender")),
                    "date": msg.get("Date", "Unknown Date"),
                    "content": content or "[No text content available]",
                }
            )
        mail.logout()

        if not emails:
            self.fail("No emails could be retrieved")

        lines = [f"Retrieved {len(emails)} recent email(s):\n"]
        for i, info in enumerate(emails, 1):
            lines += [
                f"Email {i}:",
                f"  Subject: {info['subject']}",
                f"  From: {info['from']}",
                f"  Date: {info['date']}",
                f"  Content: {info['content']}",
                "",
            ]
        return "\n".join(lines)
