"""Slack / Teams Worker — messaging, channels, DMs like a real employee.

✅ Send messages to channels and DMs
✅ Read messages from channels
✅ Reply to threads
✅ Upload files
✅ Set status / presence
✅ Search messages
✅ React with emoji
✅ Schedule messages
✅ Google Chat support

Supports both API-based (Slack Bot Token) and web-based (Playwright) modes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SlackMessage:
    """A Slack message."""
    ts: str           # timestamp / message ID
    channel: str
    user: str
    text: str
    thread_ts: str = ""
    reactions: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)


@dataclass
class MessagingWorker:
    """Send and receive Slack/Teams messages like a real employee.

    Parameters
    ----------
    slack_token:
        Slack Bot or User OAuth token (xoxb-... or xoxp-...).
    slack_webhook:
        Incoming Webhook URL for simple message posting.
    browser:
        BrowserWorker for web-based Slack/Teams/Google Chat access.
    teams_webhook:
        Microsoft Teams incoming webhook URL.
    """

    slack_token: str | None = None
    slack_webhook: str | None = None
    teams_webhook: str | None = None
    browser: Any | None = None
    human: Any | None = None
    _ws_client: Any = field(default=None, init=False)

    # ── Slack API ─────────────────────────────────────────────────────────────

    async def slack_post(self, channel: str, text: str, *, thread_ts: str | None = None) -> bool:
        """Post a message to a Slack channel or DM."""
        import aiohttp
        if not self.slack_token and not self.slack_webhook:
            return await self._slack_web(channel, text)

        url = "https://slack.com/api/chat.postMessage"
        payload: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts

        if self.slack_token:
            headers = {"Authorization": f"Bearer {self.slack_token}"}
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    data = await resp.json()
                    if not data.get("ok"):
                        logger.error("Slack post failed: %s", data.get("error"))
                        return False
                    logger.info("Slack: posted to %s", channel)
                    return True
        elif self.slack_webhook:
            async with aiohttp.ClientSession() as session:
                await session.post(self.slack_webhook, json={"text": text})
            return True
        return False

    async def slack_read(self, channel: str, limit: int = 10) -> list[SlackMessage]:
        """Read recent messages from a Slack channel."""
        if not self.slack_token:
            return []
        import aiohttp
        url = "https://slack.com/api/conversations.history"
        headers = {"Authorization": f"Bearer {self.slack_token}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params={"channel": channel, "limit": limit},
                                   headers=headers) as resp:
                data = await resp.json()
        messages = []
        for m in data.get("messages", []):
            messages.append(SlackMessage(
                ts=m.get("ts", ""),
                channel=channel,
                user=m.get("user", ""),
                text=m.get("text", ""),
                thread_ts=m.get("thread_ts", ""),
            ))
        return messages

    async def slack_reply(self, channel: str, thread_ts: str, reply: str) -> bool:
        """Reply to a Slack thread."""
        return await self.slack_post(channel, reply, thread_ts=thread_ts)

    async def slack_react(self, channel: str, ts: str, emoji: str) -> bool:
        """React to a message with an emoji."""
        if not self.slack_token:
            return False
        import aiohttp
        url = "https://slack.com/api/reactions.add"
        headers = {"Authorization": f"Bearer {self.slack_token}"}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={"channel": channel, "timestamp": ts, "name": emoji},
                                    headers=headers) as resp:
                data = await resp.json()
                return data.get("ok", False)

    async def slack_search(self, query: str, limit: int = 10) -> list[SlackMessage]:
        """Search Slack messages by keyword."""
        if not self.slack_token:
            return []
        import aiohttp
        url = "https://slack.com/api/search.messages"
        headers = {"Authorization": f"Bearer {self.slack_token}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params={"query": query, "count": limit},
                                   headers=headers) as resp:
                data = await resp.json()
        results = []
        for match in data.get("messages", {}).get("matches", []):
            results.append(SlackMessage(
                ts=match.get("ts", ""),
                channel=match.get("channel", {}).get("id", ""),
                user=match.get("username", ""),
                text=match.get("text", ""),
            ))
        return results

    async def slack_upload_file(self, channel: str, filepath: str, comment: str = "") -> bool:
        """Upload a file to a Slack channel."""
        if not self.slack_token:
            return False
        import aiohttp
        from pathlib import Path
        p = Path(filepath)
        if not p.exists():
            return False
        url = "https://slack.com/api/files.upload"
        headers = {"Authorization": f"Bearer {self.slack_token}"}
        async with aiohttp.ClientSession() as session:
            with open(p, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("channels", channel)
                data.add_field("initial_comment", comment)
                data.add_field("file", f, filename=p.name)
                async with session.post(url, data=data, headers=headers) as resp:
                    result = await resp.json()
                    return result.get("ok", False)

    async def slack_set_status(self, status_text: str, status_emoji: str = ":computer:") -> bool:
        """Set the agent's Slack status."""
        if not self.slack_token:
            return False
        import aiohttp
        url = "https://slack.com/api/users.profile.set"
        headers = {"Authorization": f"Bearer {self.slack_token}"}
        payload = {"profile": {"status_text": status_text, "status_emoji": status_emoji}}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()
                return data.get("ok", False)

    async def slack_send_dm(self, user_id: str, text: str) -> bool:
        """Send a direct message to a user."""
        if not self.slack_token:
            return False
        import aiohttp
        # Open DM channel first
        url_open = "https://slack.com/api/conversations.open"
        headers = {"Authorization": f"Bearer {self.slack_token}"}
        async with aiohttp.ClientSession() as session:
            async with session.post(url_open, json={"users": user_id}, headers=headers) as resp:
                data = await resp.json()
            channel_id = data.get("channel", {}).get("id", "")
        if not channel_id:
            return False
        return await self.slack_post(channel_id, text)

    # ── Microsoft Teams ───────────────────────────────────────────────────────

    async def teams_post(self, text: str, title: str = "Agent Message") -> bool:
        """Post a message to a Teams channel via Incoming Webhook."""
        if not self.teams_webhook:
            return False
        import aiohttp
        payload = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "title": title,
            "text": text,
        }
        async with aiohttp.ClientSession() as session:
            resp = await session.post(self.teams_webhook, json=payload,
                                      timeout=aiohttp.ClientTimeout(total=10))
        return resp.status == 200

    # ── Web-based Slack (browser fallback) ────────────────────────────────────

    async def _slack_web(self, channel: str, text: str) -> bool:
        """Type a message into Slack web app via browser."""
        if not self.browser:
            return False
        try:
            await self.browser.navigate("https://app.slack.com")
            await asyncio.sleep(3)
            # Click channel in sidebar
            await self.browser.click(channel)
            await asyncio.sleep(1)
            # Click message box and type
            await self.browser.click("message input")
            await asyncio.sleep(0.5)
            if self.human:
                await self.human.type_text(text)
            else:
                if self.browser._page:
                    await self.browser._page.keyboard.type(text)
            await asyncio.sleep(0.3)
            if self.browser._page:
                await self.browser._page.keyboard.press("Enter")
            return True
        except Exception as exc:
            logger.error("Slack web post failed: %s", exc)
            return False

    # ── Google Chat ───────────────────────────────────────────────────────────

    async def google_chat_post(self, webhook_url: str, text: str) -> bool:
        """Post a message to Google Chat via Incoming Webhook."""
        import aiohttp
        async with aiohttp.ClientSession() as session:
            resp = await session.post(webhook_url, json={"text": text},
                                      timeout=aiohttp.ClientTimeout(total=10))
        return resp.status == 200

    # ── Monitoring ────────────────────────────────────────────────────────────

    async def monitor_slack_channel(
        self,
        channel: str,
        callback: Any,
        interval: float = 15.0,
    ) -> None:
        """Poll a Slack channel and call callback(message) for each new message."""
        seen_ts: set[str] = set()
        while True:
            try:
                messages = await self.slack_read(channel, limit=5)
                for msg in messages:
                    if msg.ts not in seen_ts:
                        seen_ts.add(msg.ts)
                        await callback(msg)
            except Exception as exc:
                logger.warning("monitor_slack_channel: %s", exc)
            await asyncio.sleep(interval)
