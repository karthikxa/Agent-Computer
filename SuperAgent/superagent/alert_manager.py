"""Alert manager — threshold-based alerts and webhook notifications.

Features #84/#85/#86 — configurable thresholds, alert history, and
webhook/email dispatch inspired by Grafana Alert Engine.

Monitors agent and system metrics and fires alerts when:
  - CPU usage exceeds threshold (#84)
  - Memory usage exceeds threshold (#84)
  - Token cost exceeds budget (#86)
  - Agent stalls / goes unresponsive (#85)

Alert delivery targets:
  - Webhook (POST JSON payload)
  - Slack (via Incoming Webhook URL)
  - Email (via SMTP — optional)
  - Dashboard WebSocket broadcast

Usage::

    mgr = AlertManager(webhook_url="https://hooks.slack.com/...")
    mgr.set_threshold("cpu_pct", warn=70.0, critical=90.0)
    mgr.set_threshold("memory_mb", warn=3000, critical=7000)
    mgr.set_budget("token_cost_usd", daily_limit=10.0)

    asyncio.create_task(mgr.start_monitoring(interval=30))
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class AlertSeverity(str, Enum):
    INFO     = "info"
    WARNING  = "warning"
    CRITICAL = "critical"
    RESOLVED = "resolved"


@dataclass
class Alert:
    """A fired alert event."""
    alert_id: str
    metric: str
    severity: AlertSeverity
    value: float
    threshold: float
    message: str
    agent_id: str | None = None
    timestamp: float = field(default_factory=time.time)
    resolved: bool = False
    resolved_at: float | None = None


@dataclass
class Threshold:
    """Alert threshold config for a single metric."""
    metric: str
    warn: float
    critical: float
    unit: str = ""


# ---------------------------------------------------------------------------
# Alert Manager
# ---------------------------------------------------------------------------

class AlertManager:
    """Monitor metrics and dispatch alerts when thresholds are crossed."""

    def __init__(
        self,
        *,
        webhook_url: str | None = None,
        slack_url: str | None = None,
        smtp_host: str | None = None,
        smtp_user: str | None = None,
        smtp_pass: str | None = None,
        smtp_to: str | None = None,
    ) -> None:
        self.webhook_url = webhook_url
        self.slack_url = slack_url
        self.smtp_host = smtp_host
        self.smtp_user = smtp_user
        self.smtp_pass = smtp_pass
        self.smtp_to = smtp_to
        self._thresholds: dict[str, Threshold] = {}
        self._budgets: dict[str, float] = {}
        self._history: list[Alert] = []
        self._active_alerts: dict[str, Alert] = {}   # metric → Alert
        self._ws_clients: list[Any] = []             # Dashboard WS sockets

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_threshold(
        self,
        metric: str,
        *,
        warn: float,
        critical: float,
        unit: str = "",
    ) -> None:
        """Feature #84 — Set warn/critical thresholds for a metric."""
        self._thresholds[metric] = Threshold(metric=metric, warn=warn, critical=critical, unit=unit)
        logger.debug("AlertManager: threshold set for '%s' warn=%.1f critical=%.1f", metric, warn, critical)

    def set_budget(self, metric: str, *, daily_limit: float) -> None:
        """Feature #86 — Set a daily spend budget for a cost metric."""
        self._budgets[metric] = daily_limit
        logger.debug("AlertManager: budget set for '%s' limit=%.2f/day", metric, daily_limit)

    def register_ws_client(self, ws: Any) -> None:
        """Register a dashboard WebSocket to receive live alert broadcasts."""
        self._ws_clients.append(ws)

    def unregister_ws_client(self, ws: Any) -> None:
        if ws in self._ws_clients:
            self._ws_clients.remove(ws)

    # ------------------------------------------------------------------
    # Monitoring loop
    # ------------------------------------------------------------------

    async def start_monitoring(
        self,
        *,
        interval: float = 30.0,
        metric_fn: Any | None = None,
    ) -> None:
        """Feature #85 — Continuously poll metrics and fire alerts.

        Parameters
        ----------
        interval:
            Seconds between each metric check.
        metric_fn:
            Optional async callable returning dict[metric, float].
            Defaults to reading psutil system metrics.
        """
        logger.info("AlertManager: monitoring started (interval=%.0fs)", interval)
        while True:
            try:
                metrics = (
                    await metric_fn() if metric_fn
                    else await self._collect_system_metrics()
                )
                for metric, value in metrics.items():
                    await self._evaluate(metric, value)
            except Exception as exc:
                logger.error("AlertManager: monitoring error: %s", exc)
            await asyncio.sleep(interval)

    async def check_once(self, metrics: dict[str, float]) -> list[Alert]:
        """Evaluate a metrics snapshot once, return any new alerts."""
        new_alerts = []
        for metric, value in metrics.items():
            alert = await self._evaluate(metric, value)
            if alert:
                new_alerts.append(alert)
        return new_alerts

    # ------------------------------------------------------------------
    # History & state
    # ------------------------------------------------------------------

    def get_active_alerts(self) -> list[dict[str, Any]]:
        """Return all currently active (unresolved) alerts."""
        return [
            {
                "alert_id": a.alert_id,
                "metric": a.metric,
                "severity": a.severity,
                "value": a.value,
                "threshold": a.threshold,
                "message": a.message,
                "timestamp": a.timestamp,
                "agent_id": a.agent_id,
            }
            for a in self._active_alerts.values()
        ]

    def get_history(self, n: int = 100) -> list[dict[str, Any]]:
        """Return the last n alert events (including resolved)."""
        return [
            {
                "alert_id": a.alert_id,
                "metric": a.metric,
                "severity": a.severity.value,
                "value": a.value,
                "threshold": a.threshold,
                "message": a.message,
                "timestamp": a.timestamp,
                "resolved": a.resolved,
                "resolved_at": a.resolved_at,
                "agent_id": a.agent_id,
            }
            for a in self._history[-n:]
        ]

    # ------------------------------------------------------------------
    # Internal evaluation
    # ------------------------------------------------------------------

    async def _evaluate(self, metric: str, value: float, agent_id: str | None = None) -> Alert | None:
        """Check a single metric value against its thresholds."""
        import secrets
        threshold = self._thresholds.get(metric)
        budget = self._budgets.get(metric)

        severity: AlertSeverity | None = None
        threshold_val = 0.0

        if threshold:
            if value >= threshold.critical:
                severity = AlertSeverity.CRITICAL
                threshold_val = threshold.critical
            elif value >= threshold.warn:
                severity = AlertSeverity.WARNING
                threshold_val = threshold.warn

        if budget and value >= budget:
            severity = AlertSeverity.CRITICAL
            threshold_val = budget

        existing = self._active_alerts.get(metric)
        if severity is None:
            # Resolve active alert if metric is back to normal
            if existing:
                existing.resolved = True
                existing.resolved_at = time.time()
                existing.severity = AlertSeverity.RESOLVED
                self._active_alerts.pop(metric, None)
                await self._dispatch(existing)
            return None

        # Avoid re-firing same severity
        if existing and existing.severity == severity:
            return None

        alert = Alert(
            alert_id=secrets.token_hex(6),
            metric=metric,
            severity=severity,
            value=value,
            threshold=threshold_val,
            message=self._format_message(metric, severity, value, threshold_val),
            agent_id=agent_id,
        )
        self._active_alerts[metric] = alert
        self._history.append(alert)
        await self._dispatch(alert)
        return alert

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, alert: Alert) -> None:
        """Send alert to all configured channels."""
        payload = {
            "alert_id": alert.alert_id,
            "metric": alert.metric,
            "severity": alert.severity.value,
            "value": alert.value,
            "threshold": alert.threshold,
            "message": alert.message,
            "timestamp": alert.timestamp,
        }
        tasks = []
        if self.webhook_url:
            tasks.append(self._post_json(self.webhook_url, payload))
        if self.slack_url:
            tasks.append(self._post_json(self.slack_url, {"text": alert.message}))
        if self.smtp_host and self.smtp_to:
            tasks.append(asyncio.to_thread(self._send_email, alert))
        for ws in list(self._ws_clients):
            try:
                tasks.append(ws.send_json(payload))
            except Exception:
                pass
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.log(
            logging.WARNING if alert.severity == AlertSeverity.WARNING else logging.CRITICAL,
            "ALERT [%s] %s = %.2f (threshold %.2f): %s",
            alert.severity.value, alert.metric, alert.value, alert.threshold, alert.message,
        )

    async def _post_json(self, url: str, payload: dict[str, Any]) -> None:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                await session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10))
        except Exception as exc:
            logger.warning("AlertManager: webhook dispatch failed: %s", exc)

    def _send_email(self, alert: Alert) -> None:
        try:
            import smtplib
            from email.message import EmailMessage
            msg = EmailMessage()
            msg["Subject"] = f"[{alert.severity.value.upper()}] SuperAgent Alert: {alert.metric}"
            msg["From"] = self.smtp_user or "superagent@localhost"
            msg["To"] = self.smtp_to
            msg.set_content(alert.message)
            with smtplib.SMTP(self.smtp_host) as smtp:
                if self.smtp_user and self.smtp_pass:
                    smtp.login(self.smtp_user, self.smtp_pass)
                smtp.send_message(msg)
        except Exception as exc:
            logger.warning("AlertManager: email dispatch failed: %s", exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_message(metric: str, severity: AlertSeverity, value: float, threshold: float) -> str:
        return (
            f"[{severity.value.upper()}] {metric} = {value:.2f} "
            f"(threshold: {threshold:.2f})"
        )

    @staticmethod
    async def _collect_system_metrics() -> dict[str, float]:
        """Collect CPU, memory, disk metrics via psutil."""
        try:
            import psutil
            return await asyncio.to_thread(lambda: {
                "cpu_pct": psutil.cpu_percent(interval=1),
                "memory_mb": psutil.virtual_memory().used / 1024 / 1024,
                "memory_pct": psutil.virtual_memory().percent,
                "disk_pct": psutil.disk_usage("/").percent,
            })
        except ImportError:
            return {}
