"""Role-Based Access Control (RBAC) for SuperAgent dashboard operators.

Feature #73 — inspired by KasmVNC's permission system.

Defines roles (Admin, Operator, Viewer, Agent) and enforces them on
every dashboard API action. Roles are stored in a SQLite table and
checked via middleware.

Usage::

    rbac = RBACManager()
    rbac.create_user("alice", "hashed_pw", role=Role.ADMIN)
    rbac.create_user("bob",   "hashed_pw", role=Role.VIEWER)

    # In an aiohttp middleware:
    ok = rbac.check(token="...", action="agent.takeover")
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DB_PATH = Path(".superagent/rbac.db")


# ---------------------------------------------------------------------------
# Role definitions
# ---------------------------------------------------------------------------

class Role(str, Enum):
    ADMIN    = "admin"      # full access — create users, change permissions
    OPERATOR = "operator"   # view + send commands + change agent permissions
    VIEWER   = "viewer"     # read-only: view desktop thumbnails, metrics
    AGENT    = "agent"      # internal service role (machine-to-machine)


# Permission → minimum role required
_PERMISSION_MAP: dict[str, Role] = {
    # User management
    "user.create":          Role.ADMIN,
    "user.delete":          Role.ADMIN,
    "user.list":            Role.OPERATOR,
    # Agent permissions
    "agent.permissions.set": Role.OPERATOR,
    "agent.permissions.get": Role.VIEWER,
    # Desktop access
    "agent.view":           Role.VIEWER,
    "agent.takeover":       Role.OPERATOR,
    "agent.copilot":        Role.OPERATOR,
    # Dashboard
    "dashboard.metrics":    Role.VIEWER,
    "dashboard.alerts":     Role.VIEWER,
    "dashboard.logs":       Role.OPERATOR,
    # Container management
    "container.spawn":      Role.ADMIN,
    "container.kill":       Role.OPERATOR,
    "container.restart":    Role.OPERATOR,
    # Relay
    "relay.send":           Role.OPERATOR,
    "relay.history":        Role.VIEWER,
}

_ROLE_RANK = {Role.VIEWER: 0, Role.AGENT: 0, Role.OPERATOR: 1, Role.ADMIN: 2}


# ---------------------------------------------------------------------------
# User model
# ---------------------------------------------------------------------------

@dataclass
class User:
    user_id: str
    username: str
    role: Role
    created_at: float
    active: bool = True


# ---------------------------------------------------------------------------
# RBAC Manager
# ---------------------------------------------------------------------------

class RBACManager:
    """Manages users, tokens, and permission checks."""

    def __init__(self, db_path: str | Path = _DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------
    # DB bootstrap
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id    TEXT PRIMARY KEY,
                    username   TEXT UNIQUE NOT NULL,
                    pw_hash    TEXT NOT NULL,
                    role       TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    active     INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    token      TEXT PRIMARY KEY,
                    user_id    TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );
            """)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    # ------------------------------------------------------------------
    # User management
    # ------------------------------------------------------------------

    def create_user(self, username: str, password: str, role: Role = Role.VIEWER) -> User:
        """Create a new user account."""
        user_id = secrets.token_hex(8)
        pw_hash = self._hash_password(password)
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO users (user_id, username, pw_hash, role, created_at) VALUES (?,?,?,?,?)",
                (user_id, username, pw_hash, role.value, now),
            )
        logger.info("RBAC: created user '%s' with role %s", username, role.value)
        return User(user_id=user_id, username=username, role=role, created_at=now)

    def delete_user(self, username: str) -> bool:
        """Deactivate a user."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE users SET active=0 WHERE username=?", (username,)
            )
        return cur.rowcount > 0

    def list_users(self) -> list[User]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT user_id, username, role, created_at, active FROM users"
            ).fetchall()
        return [
            User(user_id=r[0], username=r[1], role=Role(r[2]), created_at=r[3], active=bool(r[4]))
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self, username: str, password: str) -> str | None:
        """Verify credentials and return a session token, or None on failure."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT user_id, pw_hash, active FROM users WHERE username=?",
                (username,),
            ).fetchone()
        if not row or not row[2]:
            return None
        user_id, pw_hash, _ = row
        if not self._verify_password(password, pw_hash):
            logger.warning("RBAC: failed login for '%s'", username)
            return None
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + 86400  # 24 h
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO tokens (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                (token, user_id, time.time(), expires_at),
            )
        logger.info("RBAC: issued token for user_id=%s", user_id)
        return token

    def invalidate_token(self, token: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM tokens WHERE token=?", (token,))

    # ------------------------------------------------------------------
    # Authorization
    # ------------------------------------------------------------------

    def check(self, token: str, action: str) -> bool:
        """Return True if the token holder has permission for action."""
        user = self._token_to_user(token)
        if user is None or not user.active:
            return False
        required = _PERMISSION_MAP.get(action)
        if required is None:
            # Unknown action — only admins may perform it
            return user.role == Role.ADMIN
        return _ROLE_RANK[user.role] >= _ROLE_RANK[required]

    def get_user_from_token(self, token: str) -> User | None:
        return self._token_to_user(token)

    # ------------------------------------------------------------------
    # aiohttp middleware factory
    # ------------------------------------------------------------------

    def make_middleware(self) -> Any:
        """Return an aiohttp middleware that enforces RBAC on requests."""
        from aiohttp import web

        @web.middleware
        async def rbac_middleware(request: Any, handler: Any) -> Any:
            # Endpoints that don't need auth
            public = {"/health", "/", "/dashboard/login"}
            if request.path in public:
                return await handler(request)

            token = (
                request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                or request.rel_url.query.get("token", "")
            )
            # Map HTTP method + path → action
            action = self._infer_action(request.method, request.path)
            if not self.check(token, action):
                raise web.HTTPForbidden(reason=f"Insufficient permission for '{action}'")
            return await handler(request)

        return rbac_middleware

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _token_to_user(self, token: str) -> User | None:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT u.user_id, u.username, u.role, u.created_at, u.active
                   FROM tokens t JOIN users u ON t.user_id = u.user_id
                   WHERE t.token=? AND (t.expires_at IS NULL OR t.expires_at > ?)""",
                (token, time.time()),
            ).fetchone()
        if not row:
            return None
        return User(user_id=row[0], username=row[1], role=Role(row[2]), created_at=row[3], active=bool(row[4]))

    @staticmethod
    def _hash_password(password: str) -> str:
        salt = secrets.token_hex(16)
        h = hashlib.sha256((salt + password).encode()).hexdigest()
        return f"{salt}:{h}"

    @staticmethod
    def _verify_password(password: str, pw_hash: str) -> bool:
        try:
            salt, h = pw_hash.split(":", 1)
            expected = hashlib.sha256((salt + password).encode()).hexdigest()
            return hmac.compare_digest(h, expected)
        except Exception:
            return False

    @staticmethod
    def _infer_action(method: str, path: str) -> str:
        """Map HTTP method + path to an RBAC action string."""
        mappings = [
            ("GET",  "/dashboard/metrics",      "dashboard.metrics"),
            ("GET",  "/dashboard/alerts",        "dashboard.alerts"),
            ("GET",  "/dashboard/logs",          "dashboard.logs"),
            ("GET",  "/dashboard/agents",        "agent.permissions.get"),
            ("PUT",  "/agent/",                  "agent.permissions.set"),
            ("GET",  "/agent/",                  "agent.view"),
            ("POST", "/agent/",                  "agent.copilot"),
            ("POST", "/relay/send",              "relay.send"),
            ("GET",  "/relay/history",           "relay.history"),
            ("POST", "/container/spawn",         "container.spawn"),
            ("POST", "/container/kill",          "container.kill"),
            ("POST", "/container/restart",       "container.restart"),
            ("GET",  "/users",                   "user.list"),
            ("POST", "/users",                   "user.create"),
            ("DELETE", "/users",                 "user.delete"),
        ]
        for m, prefix, action in mappings:
            if method.upper() == m and path.startswith(prefix):
                return action
        return "dashboard.metrics"  # safe default
