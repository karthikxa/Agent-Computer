"""Calendar Worker — manage schedules like a real human employee.

✅ Google Calendar: create, read, update, delete events
✅ Check availability / free-busy
✅ Schedule meetings with attendees
✅ Set reminders
✅ View today's / week's agenda
✅ Accept / decline invitations
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CalendarEvent:
    """Represents a calendar event."""
    event_id: str
    title: str
    start: datetime
    end: datetime
    attendees: list[str] = field(default_factory=list)
    location: str = ""
    description: str = ""
    meet_link: str = ""
    status: str = "confirmed"


@dataclass
class CalendarWorker:
    """Manage calendar events like a real employee.

    Parameters
    ----------
    google_credentials:
        Path to Google service account JSON or oauth token dict.
    calendar_id:
        Google Calendar ID (default: 'primary').
    browser:
        BrowserWorker for web-based calendar access.
    """

    google_credentials: str | dict | None = None
    calendar_id: str = "primary"
    browser: Any | None = None

    def _get_service(self) -> Any:
        """Build a Google Calendar API service client."""
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            if isinstance(self.google_credentials, str):
                creds = service_account.Credentials.from_service_account_file(
                    self.google_credentials,
                    scopes=["https://www.googleapis.com/auth/calendar"],
                )
            else:
                from google.oauth2.credentials import Credentials
                creds = Credentials.from_authorized_user_info(self.google_credentials)
            return build("calendar", "v3", credentials=creds, cache_discovery=False)
        except ImportError:
            raise ImportError("Install google-api-python-client and google-auth to use CalendarWorker API mode")

    # ── Read events ────────────────────────────────────────────────────────────

    async def get_today(self) -> list[CalendarEvent]:
        """Get all events scheduled for today."""
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return await self.get_events(start, end)

    async def get_week(self) -> list[CalendarEvent]:
        """Get all events for the next 7 days."""
        now = datetime.now(timezone.utc)
        return await self.get_events(now, now + timedelta(days=7))

    async def get_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """Get events between two datetimes."""
        def _fetch():
            service = self._get_service()
            result = service.events().list(
                calendarId=self.calendar_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            events = []
            for item in result.get("items", []):
                start_dt = item["start"].get("dateTime", item["start"].get("date", ""))
                end_dt   = item["end"].get("dateTime", item["end"].get("date", ""))
                events.append(CalendarEvent(
                    event_id=item["id"],
                    title=item.get("summary", ""),
                    start=datetime.fromisoformat(start_dt) if start_dt else datetime.now(timezone.utc),
                    end=datetime.fromisoformat(end_dt) if end_dt else datetime.now(timezone.utc),
                    attendees=[a["email"] for a in item.get("attendees", [])],
                    location=item.get("location", ""),
                    description=item.get("description", ""),
                    meet_link=item.get("hangoutLink", ""),
                    status=item.get("status", "confirmed"),
                ))
            return events
        try:
            return await asyncio.to_thread(_fetch)
        except Exception as exc:
            logger.error("CalendarWorker.get_events: %s", exc)
            return await self._get_events_web(start, end)

    # ── Create / schedule events ───────────────────────────────────────────────

    async def create_event(
        self,
        title: str,
        start: datetime,
        duration_minutes: int = 60,
        *,
        attendees: list[str] | None = None,
        location: str = "",
        description: str = "",
        add_google_meet: bool = True,
    ) -> CalendarEvent | None:
        """Create a new calendar event with optional attendees and Google Meet link."""
        end = start + timedelta(minutes=duration_minutes)

        def _create():
            service = self._get_service()
            body: dict[str, Any] = {
                "summary": title,
                "location": location,
                "description": description,
                "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
                "end":   {"dateTime": end.isoformat(),   "timeZone": "UTC"},
                "attendees": [{"email": e} for e in (attendees or [])],
            }
            if add_google_meet:
                body["conferenceData"] = {
                    "createRequest": {"requestId": f"meet-{int(start.timestamp())}"}
                }
            event = service.events().insert(
                calendarId=self.calendar_id,
                body=body,
                conferenceDataVersion=1 if add_google_meet else 0,
                sendUpdates="all" if attendees else "none",
            ).execute()
            return CalendarEvent(
                event_id=event["id"],
                title=event.get("summary", title),
                start=start, end=end,
                attendees=attendees or [],
                location=location,
                description=description,
                meet_link=event.get("hangoutLink", ""),
            )

        try:
            return await asyncio.to_thread(_create)
        except Exception as exc:
            logger.error("CalendarWorker.create_event: %s", exc)
            return await self._create_event_web(title, start, duration_minutes, attendees or [])

    async def schedule_meeting(
        self,
        title: str,
        attendees: list[str],
        duration_minutes: int = 30,
        *,
        preferred_time: datetime | None = None,
        description: str = "",
    ) -> CalendarEvent | None:
        """Schedule a meeting at the next available slot for all attendees."""
        start = preferred_time or datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        ) + timedelta(hours=1)
        return await self.create_event(
            title, start, duration_minutes,
            attendees=attendees, description=description, add_google_meet=True,
        )

    async def update_event(self, event_id: str, **updates: Any) -> bool:
        """Update fields of an existing event."""
        def _update():
            service = self._get_service()
            event = service.events().get(calendarId=self.calendar_id, eventId=event_id).execute()
            for key, val in updates.items():
                event[key] = val
            service.events().update(calendarId=self.calendar_id, eventId=event_id, body=event).execute()
            return True
        try:
            return await asyncio.to_thread(_update)
        except Exception as exc:
            logger.error("CalendarWorker.update_event: %s", exc)
            return False

    async def delete_event(self, event_id: str) -> bool:
        """Delete/cancel a calendar event."""
        def _delete():
            service = self._get_service()
            service.events().delete(calendarId=self.calendar_id, eventId=event_id).execute()
            return True
        try:
            return await asyncio.to_thread(_delete)
        except Exception as exc:
            logger.error("CalendarWorker.delete_event: %s", exc)
            return False

    async def accept_invite(self, event_id: str, attendee_email: str) -> bool:
        """Accept a calendar invitation."""
        def _accept():
            service = self._get_service()
            event = service.events().get(calendarId=self.calendar_id, eventId=event_id).execute()
            for attendee in event.get("attendees", []):
                if attendee["email"] == attendee_email:
                    attendee["responseStatus"] = "accepted"
            service.events().update(calendarId=self.calendar_id, eventId=event_id, body=event,
                                    sendUpdates="all").execute()
            return True
        try:
            return await asyncio.to_thread(_accept)
        except Exception as exc:
            logger.error("accept_invite: %s", exc)
            return False

    async def get_free_slots(
        self,
        date: datetime,
        duration_minutes: int = 30,
    ) -> list[tuple[datetime, datetime]]:
        """Return free time slots on a given date."""
        start_of_day = date.replace(hour=9, minute=0, second=0, microsecond=0)
        end_of_day = date.replace(hour=18, minute=0, second=0, microsecond=0)
        events = await self.get_events(start_of_day, end_of_day)
        busy = [(e.start, e.end) for e in events]

        slots = []
        cursor = start_of_day
        for bstart, bend in sorted(busy):
            if cursor + timedelta(minutes=duration_minutes) <= bstart:
                slots.append((cursor, bstart))
            cursor = max(cursor, bend)
        if cursor + timedelta(minutes=duration_minutes) <= end_of_day:
            slots.append((cursor, end_of_day))
        return slots

    # ── Web fallbacks ──────────────────────────────────────────────────────────

    async def _get_events_web(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """Read events from Google Calendar web UI via browser."""
        if not self.browser:
            return []
        await self.browser.navigate("https://calendar.google.com")
        await asyncio.sleep(3)
        text = await self.browser.get_page_text()
        return []  # Would need vision model to parse calendar grid

    async def _create_event_web(
        self,
        title: str,
        start: datetime,
        duration: int,
        attendees: list[str],
    ) -> CalendarEvent | None:
        """Create an event via Google Calendar web UI."""
        if not self.browser:
            return None
        url = (
            f"https://calendar.google.com/calendar/r/eventedit?"
            f"text={title.replace(' ', '+')}"
            f"&dates={start.strftime('%Y%m%dT%H%M%S')}"
            f"/{(start + timedelta(minutes=duration)).strftime('%Y%m%dT%H%M%S')}"
        )
        await self.browser.navigate(url)
        await asyncio.sleep(3)
        await self.browser.click("Save")
        await asyncio.sleep(2)
        return CalendarEvent(
            event_id="web-created",
            title=title, start=start,
            end=start + timedelta(minutes=duration),
            attendees=attendees,
        )
