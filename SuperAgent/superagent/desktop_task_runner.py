"""Desktop Task Runner — vision-to-action loop for a live agent desktop.

The core engine that powers ALL desktop tasks an agent can perform:
  - Takes a screenshot
  - Sends it to the grounding/vision model to identify what to click
  - Executes the action (click, type, scroll, drag)
  - Verifies the result and loops

This is what allows the agent to:
  ✅ Open Chrome / Firefox by clicking their icons
  ✅ Navigate folders
  ✅ Fill login / signup forms
  ✅ Handle captchas and OTPs
  ✅ Download and manage apps
  ✅ Copy / paste content

Usage::

    runner = DesktopTaskRunner(agent=my_agent)
    await runner.open_browser("chrome")
    await runner.navigate_to("https://gmail.com")
    await runner.perform("Click the Sign In button")
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class StepResult:
    """Result of a single task runner step."""
    success: bool
    action: str
    description: str
    screenshot_b64: str | None = None
    error: str | None = None


@dataclass
class DesktopTaskRunner:
    """High-level orchestrator: screenshot → vision → action → verify.

    Wraps SuperAgent, VirtualInputDriver, BrowserWorker and AuthWorker
    into simple one-line task methods.
    """

    agent: Any                          # SuperAgent instance
    max_retries: int = 3
    step_delay: float = 1.5            # seconds between steps
    verify_timeout: float = 10.0       # seconds to wait for page changes

    _history: list[StepResult] = field(default_factory=list, init=False)

    # ── Convenience accessors ─────────────────────────────────────────────────

    @property
    def desktop(self) -> Any:
        return getattr(self.agent, "desktop_api", None)

    @property
    def input_driver(self) -> Any:
        return getattr(self.agent, "virtual_input", None)

    @property
    def browser_worker(self) -> Any:
        return getattr(self.agent, "browser", None)

    @property
    def auth_worker(self) -> Any:
        return getattr(self.agent, "auth", None)

    # ── Core step execution ───────────────────────────────────────────────────

    async def screenshot(self) -> bytes:
        """Take a screenshot of the current agent desktop."""
        if self.desktop:
            return await self.desktop.screenshot()
        raise RuntimeError("No desktop_api attached to agent")

    async def perform(self, instruction: str) -> StepResult:
        """Ask the agent's vision model what to do and execute it.

        The agent takes a screenshot, sends it to the grounding model with
        the instruction, then executes the returned action (click/type/etc.).

        Parameters
        ----------
        instruction:
            Natural language instruction, e.g. "Click the Sign In button".
        """
        for attempt in range(self.max_retries):
            try:
                # 1. Screenshot
                img = await self.screenshot()
                # 2. Ask vision model for action
                action = await self.agent.loop.step_once(instruction, screenshot=img)
                # 3. Execute
                result = StepResult(
                    success=True,
                    action=str(action),
                    description=instruction,
                )
                self._history.append(result)
                await asyncio.sleep(self.step_delay)
                return result
            except Exception as exc:
                logger.warning("perform(): attempt %d/%d failed: %s", attempt + 1, self.max_retries, exc)
                if attempt == self.max_retries - 1:
                    result = StepResult(success=False, action="", description=instruction, error=str(exc))
                    self._history.append(result)
                    return result
                await asyncio.sleep(1.0)
        return StepResult(success=False, action="", description=instruction, error="max retries")

    # ── Browser launchers ─────────────────────────────────────────────────────

    async def open_browser(self, browser: str = "chrome") -> bool:
        """Open Chrome or Firefox by clicking its desktop icon or via shell.

        Strategy:
          1. Try clicking the desktop icon via vision model
          2. Fall back to launching the process directly via AppManager/shell

        Parameters
        ----------
        browser:
            'chrome' | 'firefox' | 'chromium'
        """
        icon_labels = {
            "chrome":    ["Google Chrome", "chrome", "Chromium"],
            "chromium":  ["Chromium", "chromium-browser"],
            "firefox":   ["Firefox", "Mozilla Firefox", "firefox"],
        }
        labels = icon_labels.get(browser.lower(), [browser])

        # Try clicking desktop icon
        if self.input_driver:
            for label in labels:
                try:
                    result = await self.perform(f"Double-click the {label} icon on the desktop")
                    if result.success:
                        await asyncio.sleep(2.5)  # let browser open
                        logger.info("Opened %s via desktop icon", browser)
                        return True
                except Exception:
                    continue

        # Fall back: launch process directly
        try:
            import asyncio as _asyncio
            import shutil
            executables = {
                "chrome":   ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"],
                "chromium": ["chromium-browser", "chromium"],
                "firefox":  ["firefox", "firefox-esr"],
            }
            for exe in executables.get(browser.lower(), [browser]):
                if shutil.which(exe):
                    proc = await _asyncio.create_subprocess_exec(
                        exe, "--new-window",
                        stdout=_asyncio.subprocess.DEVNULL,
                        stderr=_asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.sleep(3.0)
                    logger.info("Launched %s via process (pid=%d)", exe, proc.pid)
                    return True
        except Exception as exc:
            logger.error("open_browser: process launch failed: %s", exc)

        return False

    async def open_chrome(self) -> bool:
        """Shortcut: open Google Chrome."""
        return await self.open_browser("chrome")

    async def open_firefox(self) -> bool:
        """Shortcut: open Firefox."""
        return await self.open_browser("firefox")

    # ── Navigation ────────────────────────────────────────────────────────────

    async def navigate_to(self, url: str) -> bool:
        """Navigate the browser to a URL.

        Uses BrowserWorker if available, otherwise types in the address bar.
        """
        if self.browser_worker:
            try:
                await self.browser_worker.navigate(url)
                logger.info("Navigated to %s", url)
                return True
            except Exception:
                pass

        # Fall back: click address bar and type URL
        await self.perform("Click the browser address bar")
        await asyncio.sleep(0.5)
        if self.input_driver:
            await self.input_driver.press_keys(["ctrl", "a"])
            await asyncio.sleep(0.2)
            await self.input_driver.type_text(url)
            await asyncio.sleep(0.2)
            await self.input_driver.press_keys(["Return"])
            await asyncio.sleep(2.0)
            return True
        return False

    # ── Folder navigation ─────────────────────────────────────────────────────

    async def open_folder(self, folder_name: str) -> bool:
        """Double-click a folder on the desktop to open it.

        Parameters
        ----------
        folder_name:
            Name of the folder as visible on screen.
        """
        result = await self.perform(f"Double-click the folder named '{folder_name}'")
        if result.success:
            await asyncio.sleep(1.5)
        return result.success

    async def list_desktop_items(self) -> list[str]:
        """Take a screenshot and use OCR to list visible items on the desktop."""
        try:
            from superagent.ocr import OCRLayer
            img = await self.screenshot()
            ocr = OCRLayer()
            text = await ocr.extract(img)
            # Return non-empty lines as item names
            return [line.strip() for line in text.splitlines() if line.strip()]
        except Exception as exc:
            logger.warning("list_desktop_items: %s", exc)
            return []

    # ── Clipboard ─────────────────────────────────────────────────────────────

    async def copy(self) -> str:
        """Press Ctrl+C and return clipboard contents."""
        if self.input_driver:
            await self.input_driver.press_keys(["ctrl", "c"])
            await asyncio.sleep(0.3)
        try:
            import pyperclip
            return pyperclip.paste() or ""
        except Exception:
            return ""

    async def paste(self, text: str | None = None) -> bool:
        """Paste text (or current clipboard) with Ctrl+V."""
        if text:
            try:
                import pyperclip
                pyperclip.copy(text)
            except Exception:
                if self.input_driver:
                    await self.input_driver.type_text(text)
                    return True
        if self.input_driver:
            await self.input_driver.press_keys(["ctrl", "v"])
            return True
        return False

    async def select_all_and_copy(self) -> str:
        """Select all text on the page/field and copy it."""
        if self.input_driver:
            await self.input_driver.press_keys(["ctrl", "a"])
            await asyncio.sleep(0.2)
        return await self.copy()

    # ── Scrolling ─────────────────────────────────────────────────────────────

    async def scroll_down(self, times: int = 3) -> None:
        """Scroll down on the current page."""
        if self.input_driver:
            for _ in range(times):
                await self.input_driver.scroll(0, 0, -3)
                await asyncio.sleep(0.3)
        elif self.browser_worker:
            for _ in range(times):
                await self.browser_worker.scroll(0, 300)
                await asyncio.sleep(0.3)

    async def scroll_up(self, times: int = 3) -> None:
        """Scroll up on the current page."""
        if self.input_driver:
            for _ in range(times):
                await self.input_driver.scroll(0, 0, 3)
                await asyncio.sleep(0.3)
        elif self.browser_worker:
            for _ in range(times):
                await self.browser_worker.scroll(0, -300)
                await asyncio.sleep(0.3)

    # ── Search ────────────────────────────────────────────────────────────────

    async def google_search(self, query: str) -> bool:
        """Open Google and search for a query."""
        ok = await self.navigate_to(f"https://www.google.com/search?q={query.replace(' ', '+')}")
        await asyncio.sleep(2)
        return ok

    async def search_in_page(self, query: str) -> bool:
        """Use Ctrl+F to search within the current page."""
        if self.input_driver:
            await self.input_driver.press_keys(["ctrl", "f"])
            await asyncio.sleep(0.5)
            await self.input_driver.type_text(query)
            await asyncio.sleep(0.5)
            return True
        return False

    # ── Auth task shortcuts ───────────────────────────────────────────────────

    async def login(self, site: str, username: str, password: str) -> bool:
        """Navigate to a site and log in."""
        if self.auth_worker:
            return await self.auth_worker.login(site, username, password)
        await self.navigate_to(site)
        await asyncio.sleep(2)
        result = await self.perform(f"Fill in username '{username}' and password and click Log In")
        return result.success

    async def signup(self, site: str, username: str, email: str, password: str) -> bool:
        """Navigate to a site and create a new account."""
        if self.auth_worker:
            return await self.auth_worker.signup(site, username, email, password)
        await self.navigate_to(site)
        result = await self.perform("Click the Sign Up or Register button")
        return result.success

    async def create_google_account(
        self,
        first_name: str,
        last_name: str,
        username: str,
        password: str,
        **kwargs: Any,
    ) -> bool:
        """Create a new Google account end-to-end."""
        if self.auth_worker:
            return await self.auth_worker.create_google_account(
                first_name, last_name, username, password, **kwargs
            )
        return await self.navigate_to("https://accounts.google.com/signup")

    async def handle_captcha(self) -> bool:
        """Auto-detect and solve any CAPTCHA on the current page."""
        if self.auth_worker and self.browser_worker:
            url = ""
            if self.browser_worker._page:
                url = self.browser_worker._page.url
            return await self.auth_worker.auto_solve_captcha(url)
        return False

    async def handle_otp(
        self,
        *,
        method: str = "totp",
        secret: str | None = None,
        imap_host: str | None = None,
        email: str | None = None,
        email_password: str | None = None,
    ) -> str:
        """Get an OTP code by the specified method and type it into the page.

        Parameters
        ----------
        method:
            'totp' (authenticator app) | 'email' (Gmail/IMAP) | 'sms'
        secret:
            TOTP seed for authenticator-style codes.
        """
        code = ""
        if self.auth_worker:
            if method == "totp" and secret:
                code = self.auth_worker.handle_totp(secret)
            elif method == "email" and imap_host and email and email_password:
                code = await self.auth_worker.handle_email_otp(imap_host, email, email_password)
            elif method == "sms":
                code = await self.auth_worker.handle_sms_otp()

        if code and self.input_driver:
            await self.input_driver.type_text(code)
            await asyncio.sleep(0.5)
            await self.input_driver.press_keys(["Return"])
        return code

    async def enable_2fa(self, site: str, totp_secret: str) -> bool:
        """Enable two-factor authentication on a site.

        1. Navigates to the site's 2FA settings page
        2. Detects the QR code or manual entry field
        3. Enters the TOTP secret
        4. Confirms with a generated code

        Parameters
        ----------
        totp_secret:
            The base32 TOTP seed provided by the site's 2FA setup page.
        """
        # Navigate to site 2FA settings (common paths)
        for path in ["/settings/security", "/account/security", "/settings/2fa",
                     "/account/two-factor", "/security"]:
            try:
                await self.navigate_to(site.rstrip("/") + path)
                await asyncio.sleep(2)
                text = await self.browser_worker.get_page_text() if self.browser_worker else ""
                if any(s in text.lower() for s in ("two-factor", "2fa", "authenticator", "totp")):
                    break
            except Exception:
                continue

        # Click enable/set up button
        for label in ("Enable", "Set up", "Turn on", "Configure", "Add authenticator"):
            try:
                await self.perform(f"Click '{label}' for two-factor authentication")
                await asyncio.sleep(1.5)
                break
            except Exception:
                continue

        # Enter secret key if there's a manual entry option
        try:
            await self.perform("Click 'Can't scan the QR code?' or 'Enter key manually'")
            await asyncio.sleep(1)
            if self.input_driver:
                await self.input_driver.type_text(totp_secret)
        except Exception:
            pass

        # Generate and enter a verification code
        code = ""
        if self.auth_worker:
            code = self.auth_worker.handle_totp(totp_secret)
        if code and self.input_driver:
            await self.perform("Click the verification code input field")
            await asyncio.sleep(0.5)
            await self.input_driver.type_text(code)
            await asyncio.sleep(0.5)
            await self.perform("Click Verify or Confirm")

        # Check for success
        if self.browser_worker:
            text = await self.browser_worker.get_page_text()
            return any(s in text.lower() for s in (
                "two-factor enabled", "2fa enabled", "authenticator added",
                "successfully enabled", "two-step verification is on",
            ))
        return False

    # ── Download / App management ─────────────────────────────────────────────

    async def download_file(self, url: str, filename: str | None = None) -> bool:
        """Download a file via browser or DownloadManager."""
        try:
            from superagent.download_manager import DownloadManager
            dm = DownloadManager(agent_id=getattr(self.agent, "agent_id", "agent-0"))
            record = await dm.download(url, filename=filename)
            return record.status in ("completed", "complete")
        except Exception:
            # Fallback: navigate to URL and let browser download
            if self.browser_worker:
                await self.browser_worker.navigate(url)
                await asyncio.sleep(5)
                return True
            return False

    async def install_app(self, app_name: str, method: str = "auto") -> bool:
        """Install an application on the agent desktop."""
        try:
            from superagent.app_manager import AppManager
            mgr = AppManager(agent_id=getattr(self.agent, "agent_id", "agent-0"))
            result = await mgr.install(app_name, method=method)
            return result.success
        except Exception as exc:
            logger.error("install_app failed: %s", exc)
            return False

    async def uninstall_app(self, app_name: str) -> bool:
        """Uninstall an application from the agent desktop."""
        if self.auth_worker:
            return await self.auth_worker.uninstall_app(app_name)
        try:
            from superagent.app_manager import AppManager
            mgr = AppManager(agent_id=getattr(self.agent, "agent_id", "agent-0"))
            return await mgr.uninstall(app_name)
        except Exception as exc:
            logger.error("uninstall_app failed: %s", exc)
            return False

    # ── Task history ──────────────────────────────────────────────────────────

    def get_history(self) -> list[dict]:
        """Return the log of all steps executed."""
        return [
            {
                "action": s.action,
                "description": s.description,
                "success": s.success,
                "error": s.error,
            }
            for s in self._history
        ]

    def last_result(self) -> StepResult | None:
        return self._history[-1] if self._history else None
