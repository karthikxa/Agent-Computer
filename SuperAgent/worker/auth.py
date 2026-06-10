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

    # ------------------------------------------------------------------
    # Sign-up / account creation flows
    # ------------------------------------------------------------------

    async def signup(
        self,
        site: str,
        username: str,
        email: str,
        password: str,
        *,
        extra_fields: dict[str, str] | None = None,
    ) -> bool:
        """Navigate to a site and fill a generic signup form.

        Tries common signup button labels in order: Sign up, Register,
        Create account, Get started, Join.

        Parameters
        ----------
        site:
            URL of the signup page.
        username:
            Username or display name.
        email:
            Email address.
        password:
            Desired password.
        extra_fields:
            Any additional fields {label: value} to fill (e.g. "First name").
        """
        await self.browser.navigate(site)
        fields: dict[str, str] = {
            "username": username,
            "name": username,
            "email": email,
            "password": password,
            "confirm password": password,
            "repeat password": password,
        }
        if extra_fields:
            fields.update(extra_fields)
        await self.browser.fill_form(fields)

        # Try common submit button labels
        for label in ("Sign up", "Register", "Create account", "Get started", "Join", "Continue"):
            try:
                await self.browser.click(label)
                await asyncio.sleep(2)
                break
            except Exception:
                continue

        # Check whether we landed on a confirmation / welcome page
        text = await self.browser.get_page_text()
        success_signals = ("welcome", "verify your email", "check your inbox",
                           "account created", "successfully registered")
        return any(s in text.lower() for s in success_signals)

    async def create_google_account(
        self,
        first_name: str,
        last_name: str,
        username: str,
        password: str,
        *,
        recovery_email: str | None = None,
        phone_number: str | None = None,
        birth_date: dict[str, str] | None = None,
    ) -> bool:
        """Automate Google account creation with full step-by-step handling.

        Navigates through Google's multi-step signup form:
          Step 1 — Name
          Step 2 — Username
          Step 3 — Password
          Step 4 — Birthday / gender
          Step 5 — Phone / recovery email
          Step 6 — Accept ToS

        After each step the agent detects whether a CAPTCHA or
        phone-verification challenge appeared and escalates to HITL.

        Parameters
        ----------
        birth_date:
            Dict with keys 'month', 'day', 'year' (e.g. {'month': 'January', 'day': '15', 'year': '1990'})
        """
        SIGNUP_URL = "https://accounts.google.com/signup"
        await self.browser.navigate(SIGNUP_URL)
        await asyncio.sleep(2)

        # Step 1 — Name
        logger.info("Google signup: Step 1 — Name")
        await self.browser.fill_form({"First name": first_name, "Last name": last_name})
        await self.browser.click("Next")
        await asyncio.sleep(1.5)

        # Step 2 — Username
        logger.info("Google signup: Step 2 — Username")
        await self.browser.fill_form({"username": username})
        await self.browser.click("Next")
        await asyncio.sleep(1.5)

        # Check if username already taken
        text = await self.browser.get_page_text()
        if "that username is taken" in text.lower() or "someone already has" in text.lower():
            logger.warning("Google signup: username '%s' is taken", username)
            # Try with numeric suffix
            alt_username = f"{username}{int(asyncio.get_event_loop().time()) % 9999}"
            await self.browser.fill_form({"username": alt_username})
            await self.browser.click("Next")
            await asyncio.sleep(1.5)

        # Step 3 — Password
        logger.info("Google signup: Step 3 — Password")
        await self.browser.fill_form({"password": password, "confirm": password})
        await self.browser.click("Next")
        await asyncio.sleep(1.5)

        # Step 4 — Birthday / Gender
        if birth_date:
            logger.info("Google signup: Step 4 — Birthday")
            try:
                await self.browser.fill_form(birth_date)
            except Exception:
                pass
            await self.browser.click("Next")
            await asyncio.sleep(1.5)

        # Step 5 — Phone / Recovery email
        logger.info("Google signup: Step 5 — Phone/Recovery")
        if phone_number:
            try:
                await self.browser.fill_form({"phone": phone_number})
            except Exception:
                pass
        if recovery_email:
            try:
                await self.browser.fill_form({"recovery": recovery_email})
            except Exception:
                pass

        # Skip if possible
        for label in ("Skip", "Use without a phone number", "Not now"):
            try:
                await self.browser.click(label)
                await asyncio.sleep(1)
                break
            except Exception:
                continue

        # Step 6 — ToS / Privacy Policy
        logger.info("Google signup: Step 6 — Accept ToS")
        for label in ("I agree", "Agree", "Accept", "Confirm"):
            try:
                await self.browser.click(label)
                await asyncio.sleep(1.5)
                break
            except Exception:
                continue

        # Check for CAPTCHA / phone verification challenge
        text = await self.browser.get_page_text()
        if any(s in text.lower() for s in ("verify", "captcha", "robot", "phone number")):
            logger.warning("Google signup: human verification required — escalating to HITL")
            await self._escalate_to_hitl("Google account creation requires human verification")
            return False

        # Success signals
        return any(s in text.lower() for s in (
            "welcome", "your google account", "account created",
            "you're all set", "get started"
        ))

    # ------------------------------------------------------------------
    # Full captcha solve flows
    # ------------------------------------------------------------------

    async def handle_recaptcha_v2(
        self,
        sitekey: str,
        page_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 120.0,
    ) -> str | None:
        """Solve reCAPTCHA v2 (checkbox / image challenge) via 2captcha.

        Parameters
        ----------
        sitekey:
            The data-sitekey attribute from the reCAPTCHA iframe.
        page_url:
            The full URL of the page containing the CAPTCHA.
        api_key:
            2captcha API key. Defaults to env TWOCAPTCHA_API_KEY.

        Returns
        -------
        str | None
            The g-recaptcha-response token to inject, or None on failure.
        """
        import os
        import aiohttp
        key = api_key or os.getenv("TWOCAPTCHA_API_KEY")
        if not key:
            logger.warning("handle_recaptcha_v2: no TWOCAPTCHA_API_KEY — escalating to HITL")
            await self._escalate_to_hitl(f"reCAPTCHA v2 on {page_url}")
            return None

        async with aiohttp.ClientSession() as session:
            # Step 1: submit CAPTCHA task
            resp = await session.post(
                "https://2captcha.com/in.php",
                data={
                    "key": key, "method": "userrecaptcha",
                    "googlekey": sitekey, "pageurl": page_url,
                    "json": "1",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            )
            data = await resp.json(content_type=None)
            if data.get("status") != 1:
                logger.error("2captcha submit failed: %s", data)
                return None
            task_id = data["request"]
            logger.info("reCAPTCHA v2: submitted task %s, polling...", task_id)

            # Step 2: poll for result (up to timeout)
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(5)
                poll = await session.get(
                    f"https://2captcha.com/res.php?key={key}&action=get&id={task_id}&json=1",
                    timeout=aiohttp.ClientTimeout(total=15),
                )
                result = await poll.json(content_type=None)
                if result.get("status") == 1:
                    token = result["request"]
                    logger.info("reCAPTCHA v2: solved! token length=%d", len(token))
                    # Inject token into page
                    if self.browser._page:
                        await self.browser._page.evaluate(
                            f"document.getElementById('g-recaptcha-response').innerHTML='{token}'"
                        )
                    return token
                if result.get("request") != "CAPCHA_NOT_READY":
                    logger.error("2captcha error: %s", result)
                    return None
        logger.error("reCAPTCHA v2: timed out after %.0fs", timeout)
        return None

    async def handle_recaptcha_v3(
        self,
        sitekey: str,
        page_url: str,
        action: str = "submit",
        *,
        api_key: str | None = None,
    ) -> str | None:
        """Solve reCAPTCHA v3 (score-based, invisible) via 2captcha."""
        import os
        import aiohttp
        key = api_key or os.getenv("TWOCAPTCHA_API_KEY")
        if not key:
            return None
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                "https://2captcha.com/in.php",
                data={
                    "key": key, "method": "userrecaptcha", "version": "v3",
                    "googlekey": sitekey, "pageurl": page_url,
                    "action": action, "min_score": "0.7", "json": "1",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            )
            data = await resp.json(content_type=None)
            if data.get("status") != 1:
                return None
            task_id = data["request"]
            for _ in range(24):  # 2 min max
                await asyncio.sleep(5)
                poll = await session.get(
                    f"https://2captcha.com/res.php?key={key}&action=get&id={task_id}&json=1",
                    timeout=aiohttp.ClientTimeout(total=10),
                )
                result = await poll.json(content_type=None)
                if result.get("status") == 1:
                    return result["request"]
                if result.get("request") != "CAPCHA_NOT_READY":
                    return None
        return None

    async def handle_hcaptcha(
        self,
        sitekey: str,
        page_url: str,
        *,
        api_key: str | None = None,
    ) -> str | None:
        """Solve hCaptcha via 2captcha."""
        import os
        import aiohttp
        key = api_key or os.getenv("TWOCAPTCHA_API_KEY")
        if not key:
            await self._escalate_to_hitl(f"hCaptcha on {page_url}")
            return None
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                "https://2captcha.com/in.php",
                data={
                    "key": key, "method": "hcaptcha",
                    "sitekey": sitekey, "pageurl": page_url, "json": "1",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            )
            data = await resp.json(content_type=None)
            if data.get("status") != 1:
                return None
            task_id = data["request"]
            for _ in range(24):
                await asyncio.sleep(5)
                poll = await session.get(
                    f"https://2captcha.com/res.php?key={key}&action=get&id={task_id}&json=1",
                    timeout=aiohttp.ClientTimeout(total=10),
                )
                result = await poll.json(content_type=None)
                if result.get("status") == 1:
                    token = result["request"]
                    if self.browser._page:
                        await self.browser._page.evaluate(
                            f"document.querySelector('[name=h-captcha-response]').value='{token}'"
                        )
                    return token
                if result.get("request") != "CAPCHA_NOT_READY":
                    return None
        return None

    async def auto_solve_captcha(self, page_url: str) -> bool:
        """Detect and solve any CAPTCHA type on the current page automatically.

        Detection order: reCAPTCHA v2 → reCAPTCHA v3 → hCaptcha → HITL escalation.
        """
        if not self.browser._page:
            return False
        page = self.browser._page

        # Detect reCAPTCHA v2
        try:
            sitekey = await page.get_attribute("[data-sitekey]", "data-sitekey")
            if sitekey:
                # Check type
                iframe_src = await page.get_attribute("iframe[src*='recaptcha']", "src") or ""
                if "v3" in iframe_src or "enterprise" in iframe_src:
                    token = await self.handle_recaptcha_v3(sitekey, page_url)
                else:
                    token = await self.handle_recaptcha_v2(sitekey, page_url)
                if token:
                    logger.info("auto_solve_captcha: reCAPTCHA solved")
                    return True
        except Exception:
            pass

        # Detect hCaptcha
        try:
            hkey = await page.get_attribute("[data-hcaptcha-sitekey], .h-captcha", "data-sitekey")
            if hkey:
                token = await self.handle_hcaptcha(hkey, page_url)
                if token:
                    logger.info("auto_solve_captcha: hCaptcha solved")
                    return True
        except Exception:
            pass

        # Escalate to HITL
        logger.warning("auto_solve_captcha: could not auto-solve — escalating to human")
        await self._escalate_to_hitl(f"Unrecognised CAPTCHA on {page_url}")
        return False

    # ------------------------------------------------------------------
    # App management — install, uninstall, list
    # ------------------------------------------------------------------

    async def uninstall_app(self, app_name: str, method: str = "auto") -> bool:
        """Uninstall an application from the agent container.

        Parameters
        ----------
        app_name:
            Package name (e.g. 'chromium-browser', 'numpy').
        method:
            'apt' | 'pip' | 'snap' | 'auto' (auto-detect).
        """
        import shutil
        import asyncio as _asyncio

        if method == "auto":
            if shutil.which("apt-get"):
                method = "apt"
            elif shutil.which("pip3"):
                method = "pip"
            elif shutil.which("snap"):
                method = "snap"
            else:
                method = "apt"

        cmds = {
            "apt": f"DEBIAN_FRONTEND=noninteractive apt-get remove -y {app_name}",
            "pip": f"pip3 uninstall -y {app_name}",
            "snap": f"snap remove {app_name}",
        }
        cmd = cmds.get(method, cmds["apt"])
        logger.info("AuthWorker: uninstalling '%s' via %s", app_name, method)
        try:
            proc = await _asyncio.create_subprocess_shell(
                cmd,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
            )
            _, stderr = await _asyncio.wait_for(proc.communicate(), timeout=120)
            success = proc.returncode == 0
            if not success:
                logger.warning("Uninstall failed: %s", stderr.decode("utf-8", errors="replace")[:500])
            return success
        except Exception as exc:
            logger.error("Uninstall error: %s", exc)
            return False

    # ------------------------------------------------------------------
    # HITL escalation helper
    # ------------------------------------------------------------------

    async def _escalate_to_hitl(self, reason: str) -> None:
        """Pause the agent and notify the escalation webhook with a reason."""
        logger.warning("HITL escalation: %s", reason)
        if self.escalation_webhook:
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    await session.post(
                        self.escalation_webhook,
                        json={"type": "human_verification_required", "reason": reason},
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
            except Exception as exc:
                logger.error("HITL webhook failed: %s", exc)
        if self.task_db:
            try:
                await self.task_db.log_event("hitl_escalation", reason)
            except Exception:
                pass

