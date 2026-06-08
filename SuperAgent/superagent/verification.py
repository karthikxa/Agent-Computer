"""Human verification helpers."""

from __future__ import annotations

import base64
import hmac
import hashlib
import imaplib
import struct
import time
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class VerificationResult:
    """Verification outcome."""

    ok: bool
    reason: str = ""
    metadata: dict[str, Any] | None = None


class HumanVerificationHandler:
    """Verify human approval through TOTP or email codes."""

    def __init__(self, totp_secrets: list[str] | None = None, *, email_address: str | None = None, imap_host: str | None = None, imap_user: str | None = None, imap_app_password: str | None = None) -> None:
        self.totp_secrets = totp_secrets or []
        self.email_address = email_address
        self.imap_host = imap_host
        self.imap_user = imap_user
        self.imap_app_password = imap_app_password

    def generate_totp(self, secret: str, *, interval: int = 30, digits: int = 6) -> str:
        """Generate a TOTP token from a base32 secret."""

        key = base64.b32decode(secret.upper() + "=" * ((8 - len(secret) % 8) % 8))
        counter = int(time.time()) // interval
        message = struct.pack(">Q", counter)
        digest = hmac.new(key, message, hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
        return str(code % (10 ** digits)).zfill(digits)

    def verify_totp(self, token: str, *, window: int = 1) -> bool:
        """Verify a token against any configured secret."""

        for secret in self.totp_secrets:
            for offset in range(-window, window + 1):
                expected = self._generate_totp_at(secret, offset)
                if hmac.compare_digest(expected, token):
                    return True
        return False

    def _generate_totp_at(self, secret: str, offset: int, *, interval: int = 30, digits: int = 6) -> str:
        key = base64.b32decode(secret.upper() + "=" * ((8 - len(secret) % 8) % 8))
        counter = int(time.time() + offset * interval) // interval
        message = struct.pack(">Q", counter)
        digest = hmac.new(key, message, hashlib.sha1).digest()
        start = digest[-1] & 0x0F
        code = struct.unpack(">I", digest[start:start + 4])[0] & 0x7FFFFFFF
        return str(code % (10 ** digits)).zfill(digits)

    def fetch_email_code(self) -> str | None:
        """Fetch the latest one-time code from a verification mailbox."""

        if not self.imap_host or not self.imap_user or not self.imap_app_password:
            return None
        try:
            with imaplib.IMAP4_SSL(self.imap_host) as imap:
                imap.login(self.imap_user, self.imap_app_password)
                imap.select("INBOX")
                _, data = imap.search(None, "ALL")
                if not data or not data[0]:
                    return None
                latest_id = data[0].split()[-1]
                _, message_data = imap.fetch(latest_id, "(RFC822)")
                if not message_data:
                    return None
                payload = message_data[0][1].decode("utf-8", errors="ignore")
                digits = "".join(ch for ch in payload if ch.isdigit())
                return digits[:6] if len(digits) >= 6 else None
        except Exception:
            return None

    def verify(self, token: str) -> VerificationResult:
        """Verify a supplied token."""

        if self.verify_totp(token):
            return VerificationResult(ok=True, reason="totp")
        email_code = self.fetch_email_code()
        if email_code and hmac.compare_digest(email_code, token):
            return VerificationResult(ok=True, reason="email")
        return VerificationResult(ok=False, reason="not verified")

    def handle_totp(self, site: str, totp_secrets: dict) -> str:
        """
        Generate current TOTP code for YOUR OWN account.
        Requires the secret key saved when you set up 2FA on your account.

        Usage: store secret in AgentConfig.totp_secrets = {"github.com": "SECRET"}
        """
        secret = totp_secrets.get(site) or totp_secrets.get("default", "")
        if not secret:
            raise ValueError(
                f"No TOTP secret configured for {site}. "
                "Add it to AgentConfig.totp_secrets when setting up your own account."
            )
        try:
            import pyotp

            return pyotp.TOTP(secret).now()
        except ImportError:
            import hmac, hashlib, struct, time

            secret_bytes = __import__("base64").b32decode(
                secret.upper().replace(" ", ""), casefold=True
            )
            counter = int(time.time()) // 30
            msg = struct.pack(">Q", counter)
            digest = hmac.new(secret_bytes, msg, hashlib.sha1).digest()
            offset = digest[-1] & 0x0F
            code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
            return str(code % 1_000_000).zfill(6)

    async def handle_email_otp(
        self,
        imap_host: str,
        email_address: str,
        app_password: str,
        wait_seconds: int = 60,
    ) -> str:
        """
        Read OTP code from YOUR OWN email inbox via IMAP.
        Use a dedicated Gmail account for each agent.
        Create a Gmail App Password at: myaccount.google.com/apppasswords
        """
        import asyncio, re, time

        if not email_address or not app_password:
            raise ValueError(
                "Set email_address and email_app_password in AgentConfig. "
                "Create a Gmail App Password at myaccount.google.com/apppasswords"
            )
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            try:
                with imaplib.IMAP4_SSL(imap_host) as imap:
                    imap.login(email_address, app_password)
                    imap.select("INBOX")
                    _, data = imap.search(None, "UNSEEN")
                    if data and data[0]:
                        ids = data[0].split()
                        for uid in reversed(ids[-5:]):
                            _, raw = imap.fetch(uid, "(RFC822)")
                            if not raw or not raw[0]:
                                continue
                            body = raw[0][1].decode("utf-8", errors="replace")
                            codes = re.findall(r"(?<!\d)(\d{4,8})(?!\d)", body)
                            if codes:
                                return codes[0]
            except Exception:
                pass
            await asyncio.sleep(3)
        raise TimeoutError(
            f"No OTP email received within {wait_seconds}s. "
            f"Check {email_address} inbox."
        )

    async def handle_file_dialog(self, file_path: str) -> bool:
        """
        Handle OS native file picker dialog opened by the agent's own browser.
        Uses xdotool to type the file path and press Enter.
        Works for GTK, Qt, and browser file dialogs on Linux/Docker.
        """
        import asyncio, subprocess

        await asyncio.sleep(1.0)

        window_id = None
        for title in ["Open", "Upload", "Select File", "Choose File", "Save"]:
            try:
                result = subprocess.run(
                    ["xdotool", "search", "--name", title],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if result.stdout.strip():
                    window_id = result.stdout.strip().split()[0]
                    break
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue

        if window_id:
            try:
                subprocess.run(["xdotool", "windowfocus", "--sync", window_id], timeout=3)
                await asyncio.sleep(0.2)
                subprocess.run(
                    ["xdotool", "type", "--clearmodifiers", file_path],
                    timeout=5,
                )
                await asyncio.sleep(0.2)
                subprocess.run(["xdotool", "key", "Return"], timeout=3)
                return True
            except Exception:
                return False
        return False
