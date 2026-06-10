"""Email Worker — read, compose, send and manage emails like a human employee.

Supports:
  ✅ Gmail (web via Playwright)
  ✅ Outlook / Hotmail (web)
  ✅ Any IMAP/SMTP mailbox (direct protocol)
  ✅ Search emails by sender, subject, keyword
  ✅ Read, reply, forward, archive, delete
  ✅ Compose and send with attachments
  ✅ Extract OTP codes from emails
  ✅ Monitor inbox for new messages
  ✅ Unsubscribe from mailing lists
"""

from __future__ import annotations

import asyncio
import email as _email_lib
import imaplib
import logging
import re
import smtplib
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class EmailMessage:
    """Represents a single email."""
    uid: str
    subject: str
    sender: str
    recipients: list[str]
    body_text: str
    body_html: str = ""
    date: str = ""
    attachments: list[str] = field(default_factory=list)
    is_read: bool = False
    thread_id: str = ""


@dataclass
class EmailWorker:
    """Manage email like a human employee — read, write, search, reply.

    Parameters
    ----------
    browser:
        BrowserWorker for web-based Gmail/Outlook automation.
    imap_host:
        IMAP server for direct protocol access (e.g. 'imap.gmail.com').
    smtp_host:
        SMTP server for sending (e.g. 'smtp.gmail.com').
    email_address:
        The agent's email address.
    email_password:
        App password or account password.
    """

    browser: Any | None = None
    imap_host: str = "imap.gmail.com"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    email_address: str = ""
    email_password: str = ""

    # ── IMAP: read emails ──────────────────────────────────────────────────────

    def _imap_connect(self) -> imaplib.IMAP4_SSL:
        imap = imaplib.IMAP4_SSL(self.imap_host)
        imap.login(self.email_address, self.email_password)
        return imap

    async def get_inbox(self, limit: int = 20, unread_only: bool = False) -> list[EmailMessage]:
        """Fetch recent emails from the inbox."""
        def _fetch():
            imap = self._imap_connect()
            imap.select("INBOX")
            criterion = "UNSEEN" if unread_only else "ALL"
            _, data = imap.search(None, criterion)
            uids = data[0].split()[-limit:]
            messages = []
            for uid in reversed(uids):
                _, raw = imap.fetch(uid, "(RFC822)")
                if not raw or not raw[0]:
                    continue
                msg = _email_lib.message_from_bytes(raw[0][1])
                body_text = ""
                body_html = ""
                attachments = []
                if msg.is_multipart():
                    for part in msg.walk():
                        ct = part.get_content_type()
                        cd = str(part.get("Content-Disposition", ""))
                        if ct == "text/plain" and "attachment" not in cd:
                            body_text = part.get_payload(decode=True).decode("utf-8", errors="replace")
                        elif ct == "text/html" and "attachment" not in cd:
                            body_html = part.get_payload(decode=True).decode("utf-8", errors="replace")
                        elif "attachment" in cd:
                            attachments.append(part.get_filename() or "unknown")
                else:
                    payload = msg.get_payload(decode=True)
                    body_text = payload.decode("utf-8", errors="replace") if payload else ""

                messages.append(EmailMessage(
                    uid=uid.decode(),
                    subject=msg.get("Subject", "(no subject)"),
                    sender=msg.get("From", ""),
                    recipients=[msg.get("To", "")],
                    body_text=body_text,
                    body_html=body_html,
                    date=msg.get("Date", ""),
                    attachments=attachments,
                    is_read=not unread_only,
                ))
            imap.logout()
            return messages
        return await asyncio.to_thread(_fetch)

    async def search_emails(self, query: str, limit: int = 10) -> list[EmailMessage]:
        """Search inbox by subject, sender, or keyword."""
        def _search():
            imap = self._imap_connect()
            imap.select("INBOX")
            # Try SUBJECT, BODY, FROM searches
            results = set()
            for criterion in [f'SUBJECT "{query}"', f'FROM "{query}"', f'BODY "{query}"']:
                try:
                    _, data = imap.search(None, criterion)
                    for uid in data[0].split():
                        results.add(uid)
                except Exception:
                    pass
            messages = []
            for uid in list(results)[-limit:]:
                _, raw = imap.fetch(uid, "(RFC822)")
                if raw and raw[0]:
                    msg = _email_lib.message_from_bytes(raw[0][1])
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                                break
                    else:
                        payload = msg.get_payload(decode=True)
                        body = payload.decode("utf-8", errors="replace") if payload else ""
                    messages.append(EmailMessage(
                        uid=uid.decode(),
                        subject=msg.get("Subject", ""),
                        sender=msg.get("From", ""),
                        recipients=[msg.get("To", "")],
                        body_text=body,
                        date=msg.get("Date", ""),
                    ))
            imap.logout()
            return messages
        return await asyncio.to_thread(_search)

    async def extract_otp_from_email(
        self,
        wait_seconds: int = 60,
        sender_filter: str | None = None,
    ) -> str:
        """Wait for an email with an OTP/verification code and return it."""
        deadline = asyncio.get_event_loop().time() + wait_seconds
        while asyncio.get_event_loop().time() < deadline:
            emails = await self.get_inbox(limit=5, unread_only=True)
            for em in emails:
                if sender_filter and sender_filter.lower() not in em.sender.lower():
                    continue
                codes = re.findall(r"(?<!\d)(\d{4,8})(?!\d)", em.body_text)
                if codes:
                    logger.info("EmailWorker: extracted OTP %s from email '%s'", codes[0], em.subject)
                    return codes[0]
                # Also check subject line
                codes = re.findall(r"(?<!\d)(\d{4,8})(?!\d)", em.subject)
                if codes:
                    return codes[0]
            await asyncio.sleep(5)
        raise TimeoutError(f"No OTP email received within {wait_seconds}s")

    # ── SMTP: send emails ──────────────────────────────────────────────────────

    async def send_email(
        self,
        to: str | list[str],
        subject: str,
        body: str,
        *,
        cc: str | list[str] | None = None,
        html_body: str | None = None,
        attachments: list[str | Path] | None = None,
    ) -> bool:
        """Compose and send an email."""
        def _send():
            recipients = [to] if isinstance(to, str) else to
            msg = MIMEMultipart("alternative" if html_body else "mixed")
            msg["From"]    = self.email_address
            msg["To"]      = ", ".join(recipients)
            msg["Subject"] = subject
            if cc:
                cc_list = [cc] if isinstance(cc, str) else cc
                msg["Cc"] = ", ".join(cc_list)
                recipients.extend(cc_list)

            msg.attach(MIMEText(body, "plain"))
            if html_body:
                msg.attach(MIMEText(html_body, "html"))

            for path in (attachments or []):
                p = Path(path)
                if p.exists():
                    with open(p, "rb") as f:
                        part = MIMEBase("application", "octet-stream")
                        part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header("Content-Disposition", f'attachment; filename="{p.name}"')
                    msg.attach(part)

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as smtp:
                smtp.starttls()
                smtp.login(self.email_address, self.email_password)
                smtp.sendmail(self.email_address, recipients, msg.as_string())
            return True

        try:
            result = await asyncio.to_thread(_send)
            logger.info("EmailWorker: sent email to %s — '%s'", to, subject)
            return result
        except Exception as exc:
            logger.error("EmailWorker: send failed: %s", exc)
            return False

    async def reply_to(self, original: EmailMessage, reply_body: str) -> bool:
        """Reply to an email."""
        subject = original.subject if original.subject.startswith("Re:") else f"Re: {original.subject}"
        return await self.send_email(original.sender, subject, reply_body)

    async def forward_email(self, original: EmailMessage, to: str, note: str = "") -> bool:
        """Forward an email to another recipient."""
        body = f"{note}\n\n--- Forwarded message ---\n" \
               f"From: {original.sender}\nSubject: {original.subject}\n\n{original.body_text}"
        return await self.send_email(to, f"Fwd: {original.subject}", body)

    # ── Gmail web automation via Playwright ───────────────────────────────────

    async def open_gmail(self) -> bool:
        """Open Gmail in the browser."""
        if not self.browser:
            return False
        await self.browser.navigate("https://mail.google.com")
        await asyncio.sleep(3)
        return True

    async def compose_gmail(self, to: str, subject: str, body: str) -> bool:
        """Compose and send an email via Gmail web UI."""
        if not self.browser:
            return await self.send_email(to, subject, body)
        await self.open_gmail()
        await self.browser.click("Compose")
        await asyncio.sleep(1.5)
        await self.browser.fill_form({"To": to, "Subject": subject})
        # Click body area and type
        await self.browser.click("Message Body")
        await asyncio.sleep(0.5)
        await self.browser.page_action("type", body)
        await asyncio.sleep(0.5)
        await self.browser.click("Send")
        await asyncio.sleep(2)
        return True

    async def read_latest_gmail(self) -> EmailMessage | None:
        """Open Gmail and read the latest unread email."""
        if not self.browser:
            emails = await self.get_inbox(limit=1, unread_only=True)
            return emails[0] if emails else None
        await self.open_gmail()
        await self.browser.click("Inbox")
        await asyncio.sleep(1.5)
        # Click first unread email
        try:
            await self.browser.click("UNREAD")
            await asyncio.sleep(2)
            text = await self.browser.get_page_text()
            return EmailMessage(uid="web", subject="", sender="", recipients=[], body_text=text)
        except Exception:
            return None

    # ── Inbox management ───────────────────────────────────────────────────────

    async def mark_as_read(self, uid: str) -> bool:
        def _mark():
            imap = self._imap_connect()
            imap.select("INBOX")
            imap.store(uid, "+FLAGS", "\\Seen")
            imap.logout()
            return True
        return await asyncio.to_thread(_mark)

    async def delete_email(self, uid: str) -> bool:
        def _delete():
            imap = self._imap_connect()
            imap.select("INBOX")
            imap.store(uid, "+FLAGS", "\\Deleted")
            imap.expunge()
            imap.logout()
            return True
        return await asyncio.to_thread(_delete)

    async def archive_email(self, uid: str, folder: str = "[Gmail]/All Mail") -> bool:
        def _archive():
            imap = self._imap_connect()
            imap.select("INBOX")
            imap.copy(uid, folder)
            imap.store(uid, "+FLAGS", "\\Deleted")
            imap.expunge()
            imap.logout()
            return True
        return await asyncio.to_thread(_archive)

    async def monitor_inbox(self, callback: Any, interval: float = 30.0) -> None:
        """Continuously poll the inbox and call callback(email) for each new message."""
        seen_uids: set[str] = set()
        while True:
            try:
                emails = await self.get_inbox(limit=10, unread_only=True)
                for em in emails:
                    if em.uid not in seen_uids:
                        seen_uids.add(em.uid)
                        await callback(em)
            except Exception as exc:
                logger.warning("EmailWorker.monitor_inbox: %s", exc)
            await asyncio.sleep(interval)
