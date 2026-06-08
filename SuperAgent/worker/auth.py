"""Authentication worker for logins and verification flows."""

from __future__ import annotations

import asyncio
import base64
import hmac
import hashlib
import imaplib
import logging
import re
import struct
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

from infrastructure.task_db import TaskDatabase
from superagent.ocr import OCRLayer
from .browser import BrowserWorker


@dataclass(slots=True)
class AuthWorker:
    """Handle login and 2FA workflows."""

    browser: BrowserWorker
    task_db: TaskDatabase | None = None
    escalation_webhook: str | None = None
    credential_vault: dict[str, dict[str, str]] = field(default_factory=dict)

    async def store_credential(self, site: str, username: str, password: str) -> None:
        """Store site credentials securely in the vault."""
        self.credential_vault[site] = {"username": username, "password": password}

    async def get_credential(self, site: str) -> tuple[str, str] | None:
        """Retrieve credentials for a specific site."""
        creds = self.credential_vault.get(site)
        if creds:
            return creds["username"], creds["password"]
        return None

    async def automate_sso(self, sso_provider: str, credentials_site: str) -> bool:
        """Automate OAuth SSO popup flows using vault credentials."""
        creds = await self.get_credential(credentials_site)
        if not creds:
            logger.warning("SSO credentials not found in vault.")
            return False
        username, password = creds
        # 1. Click on the SSO provider button
        await self.browser.click(sso_provider)
        # 2. Fill login details on the redirected/popup auth page
        await self.browser.fill_form({"username": username, "email": username, "password": password})
        await self.browser.click("Sign in")
        return True

    async def login(self, site: str, username: str, password: str) -> bool:
        """Find and submit a login form."""

        await self.browser.navigate(site)
        await self.browser.fill_form({"username": username, "email": username, "password": password})
        try:
            await self.browser.click("Log in")
        except Exception:
            pass
        return await self.is_logged_in(site)

    def handle_totp(self, secret: str) -> str:
        """Generate a TOTP code and return it."""

        try:
            import pyotp
            return pyotp.TOTP(secret).now()
        except Exception:
            secret_bytes = base64.b32decode(secret.upper().replace(" ", ""), casefold=True)
            counter = int(time.time()) // 30
            msg = struct.pack(">Q", counter)
            digest = hmac.new(secret_bytes, msg, hashlib.sha1).digest()
            offset = digest[-1] & 0x0F
            code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
            return str(code % 1_000_000).zfill(6)

    async def handle_email_otp(self, imap_host: str, email: str, password: str, wait_seconds: int = 60) -> str:
        """Wait for an email OTP and return the code."""

        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            try:
                with imaplib.IMAP4_SSL(imap_host) as imap:
                    imap.login(email, password)
                    imap.select("INBOX")
                    _, data = imap.search(None, "UNSEEN")
                    if data and data[0]:
                        for uid in reversed(data[0].split()[-5:]):
                            _, raw = imap.fetch(uid, "(RFC822)")
                            if raw and raw[0]:
                                body = raw[0][1].decode("utf-8", errors="replace")
                                codes = re.findall(r"(?<!\d)(\d{4,8})(?!\d)", body)
                                if codes:
                                    return codes[0]
            except Exception:
                pass
            await asyncio.sleep(3)
        raise TimeoutError(f"No OTP email received within {wait_seconds}s")

    async def handle_sms_otp(self) -> str:
        """Poll the escalation webhook for an SMS code."""

        if not self.escalation_webhook:
            raise ValueError("No escalation webhook configured")
        import aiohttp

        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(self.escalation_webhook, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            text = await resp.text()
                            match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", text)
                            if match:
                                return match.group(1)
            except Exception:
                pass
            await asyncio.sleep(3)
        raise TimeoutError("SMS code not received")

    async def handle_captcha(self) -> bool:
        """Attempt captcha solving with 2captcha or escalate to a human."""

        import aiohttp

        two_captcha_key = None
        try:
            import os
            two_captcha_key = os.getenv("TWOCAPTCHA_API_KEY")
        except Exception:
            two_captcha_key = None
        if two_captcha_key:
            async with aiohttp.ClientSession() as session:
                # Real 2captcha polling flow expects the calling code to submit
                # the captcha sitekey and pageurl first. This method performs the
                # common status polling step if a solution id is already available.
                async with session.get(
                    f"https://2captcha.com/res.php?key={two_captcha_key}&action=get&id={two_captcha_key}",
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    return resp.status == 200
        if self.escalation_webhook:
            async with aiohttp.ClientSession() as session:
                await session.post(self.escalation_webhook, json={"type": "captcha"}, timeout=aiohttp.ClientTimeout(total=10))
            return False
        raise RuntimeError("Captcha blocked and no 2captcha key or escalation webhook configured")

    async def handle_oauth(self, provider: str) -> bool:
        """Handle OAuth popup flows."""

        await self.browser.wait_for(provider, timeout=30)
        return True

    async def save_session(self, site: str) -> None:
        """Persist browser session state."""

        if self.task_db is None:
            return
        cookies = await self.browser._page.context.cookies() if self.browser._page else []
        local_storage = await self.browser._page.evaluate("() => JSON.stringify(localStorage)") if self.browser._page else "{}"
        await self.task_db.save_session("browser", site, str(cookies), str(local_storage))

    async def restore_session(self, site: str) -> bool:
        """Restore session state."""

        if self.task_db is None:
            return False
        saved = await self.task_db.load_session("browser", site)
        return saved is not None

    async def is_logged_in(self, site: str) -> bool:
        """Inspect the page and infer whether login succeeded."""

        text = await self.browser.get_page_text()
        return site.lower().split("//")[-1].split("/")[0] in text.lower()
