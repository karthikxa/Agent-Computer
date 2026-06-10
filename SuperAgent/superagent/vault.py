"""Login credential vault for per-agent secure storage.

Feature #77 — encrypted per-agent credential store.

Stores login credentials (username, password, TOTP seed, OAuth tokens)
encrypted at rest using Fernet symmetric encryption.
Each agent gets its own namespace within a shared SQLite database.

Usage::

    vault = CredentialVault(agent_id="agent-1")
    vault.store("github.com", username="alice", password="s3cr3t")
    vault.store("google.com", username="alice@g.com", totp_seed="BASE32SEED")

    cred = vault.get("github.com")
    print(cred.username, cred.password)

    # OAuth tokens
    vault.store_token("google.com", access_token="...", refresh_token="...", expires_at=1234567890)
    token = vault.get_token("google.com")
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DB_PATH = Path(".superagent/vault.db")
_VAULT_KEY_ENV = "SUPERAGENT_VAULT_KEY"


# ---------------------------------------------------------------------------
# Credential models
# ---------------------------------------------------------------------------

@dataclass
class Credential:
    """A stored login credential."""
    site: str
    username: str
    password: str
    totp_seed: str = ""
    notes: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class OAuthToken:
    """A stored OAuth token set."""
    site: str
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0
    scope: str = ""
    token_type: str = "Bearer"

    @property
    def is_expired(self) -> bool:
        return self.expires_at > 0 and time.time() > self.expires_at


# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------

def _get_fernet(key: bytes | None = None) -> Any:
    """Return a Fernet cipher. Generates a key if none provided."""
    try:
        from cryptography.fernet import Fernet
        if key is None:
            env_key = os.getenv(_VAULT_KEY_ENV)
            if env_key:
                key = base64.urlsafe_b64decode(env_key.encode())
            else:
                # Derive a key from a local keyfile
                keyfile = Path(".superagent/.vault_key")
                if keyfile.exists():
                    key = base64.urlsafe_b64decode(keyfile.read_bytes())
                else:
                    key = Fernet.generate_key()
                    keyfile.parent.mkdir(parents=True, exist_ok=True)
                    keyfile.write_bytes(base64.urlsafe_b64encode(key))
                    keyfile.chmod(0o600)
        return Fernet(key if len(key) == 32 else base64.urlsafe_b64decode(key))
    except ImportError:
        return None  # Fallback: store unencrypted with warning


def _encrypt(data: str, fernet: Any) -> str:
    if fernet is None:
        return data  # no encryption available
    return fernet.encrypt(data.encode()).decode()


def _decrypt(data: str, fernet: Any) -> str:
    if fernet is None:
        return data
    return fernet.decrypt(data.encode()).decode()


# ---------------------------------------------------------------------------
# Vault
# ---------------------------------------------------------------------------

class CredentialVault:
    """Per-agent encrypted credential store backed by SQLite."""

    def __init__(
        self,
        agent_id: str,
        db_path: str | Path = _DB_PATH,
        *,
        encryption_key: bytes | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fernet = _get_fernet(encryption_key)
        if self._fernet is None:
            logger.warning(
                "CredentialVault: cryptography package not installed — "
                "credentials stored in PLAINTEXT. Install with: pip install cryptography"
            )
        self._init_db()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS credentials (
                    agent_id   TEXT NOT NULL,
                    site       TEXT NOT NULL,
                    username   TEXT NOT NULL,
                    password   TEXT NOT NULL,
                    totp_seed  TEXT DEFAULT '',
                    notes      TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (agent_id, site)
                );
                CREATE TABLE IF NOT EXISTS oauth_tokens (
                    agent_id      TEXT NOT NULL,
                    site          TEXT NOT NULL,
                    access_token  TEXT NOT NULL,
                    refresh_token TEXT DEFAULT '',
                    expires_at    REAL DEFAULT 0,
                    scope         TEXT DEFAULT '',
                    token_type    TEXT DEFAULT 'Bearer',
                    updated_at    REAL NOT NULL,
                    PRIMARY KEY (agent_id, site)
                );
            """)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    # ------------------------------------------------------------------
    # Credential CRUD
    # ------------------------------------------------------------------

    def store(
        self,
        site: str,
        *,
        username: str,
        password: str,
        totp_seed: str = "",
        notes: str = "",
    ) -> None:
        """Store or update a login credential."""
        now = time.time()
        enc_pw = _encrypt(password, self._fernet)
        enc_totp = _encrypt(totp_seed, self._fernet) if totp_seed else ""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO credentials
                   (agent_id, site, username, password, totp_seed, notes, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(agent_id, site) DO UPDATE SET
                   username=excluded.username,
                   password=excluded.password,
                   totp_seed=excluded.totp_seed,
                   notes=excluded.notes,
                   updated_at=excluded.updated_at""",
                (self.agent_id, site, username, enc_pw, enc_totp, notes, now, now),
            )
        logger.debug("Vault: stored credential for %s@%s", username, site)

    def get(self, site: str) -> Credential | None:
        """Retrieve a credential by site."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT username, password, totp_seed, notes, created_at, updated_at "
                "FROM credentials WHERE agent_id=? AND site=?",
                (self.agent_id, site),
            ).fetchone()
        if not row:
            return None
        return Credential(
            site=site,
            username=row[0],
            password=_decrypt(row[1], self._fernet),
            totp_seed=_decrypt(row[2], self._fernet) if row[2] else "",
            notes=row[3],
            created_at=row[4],
            updated_at=row[5],
        )

    def delete(self, site: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM credentials WHERE agent_id=? AND site=?",
                (self.agent_id, site),
            )
        return cur.rowcount > 0

    def list_sites(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT site FROM credentials WHERE agent_id=?", (self.agent_id,)
            ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # OAuth token CRUD
    # ------------------------------------------------------------------

    def store_token(
        self,
        site: str,
        *,
        access_token: str,
        refresh_token: str = "",
        expires_at: float = 0.0,
        scope: str = "",
        token_type: str = "Bearer",
    ) -> None:
        """Store or update an OAuth token set."""
        now = time.time()
        enc_at = _encrypt(access_token, self._fernet)
        enc_rt = _encrypt(refresh_token, self._fernet) if refresh_token else ""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO oauth_tokens
                   (agent_id, site, access_token, refresh_token, expires_at, scope, token_type, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(agent_id, site) DO UPDATE SET
                   access_token=excluded.access_token,
                   refresh_token=excluded.refresh_token,
                   expires_at=excluded.expires_at,
                   scope=excluded.scope,
                   token_type=excluded.token_type,
                   updated_at=excluded.updated_at""",
                (self.agent_id, site, enc_at, enc_rt, expires_at, scope, token_type, now),
            )

    def get_token(self, site: str) -> OAuthToken | None:
        """Retrieve an OAuth token by site."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT access_token, refresh_token, expires_at, scope, token_type "
                "FROM oauth_tokens WHERE agent_id=? AND site=?",
                (self.agent_id, site),
            ).fetchone()
        if not row:
            return None
        return OAuthToken(
            site=site,
            access_token=_decrypt(row[0], self._fernet),
            refresh_token=_decrypt(row[1], self._fernet) if row[1] else "",
            expires_at=row[2],
            scope=row[3],
            token_type=row[4],
        )

    def delete_token(self, site: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM oauth_tokens WHERE agent_id=? AND site=?",
                (self.agent_id, site),
            )
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Export / import
    # ------------------------------------------------------------------

    def export_json(self, *, include_passwords: bool = False) -> str:
        """Export credentials as JSON (passwords redacted by default)."""
        sites = self.list_sites()
        data = []
        for site in sites:
            cred = self.get(site)
            if cred:
                entry: dict[str, Any] = {
                    "site": site,
                    "username": cred.username,
                    "notes": cred.notes,
                }
                if include_passwords:
                    entry["password"] = cred.password
                    entry["totp_seed"] = cred.totp_seed
                data.append(entry)
        return json.dumps({"agent_id": self.agent_id, "credentials": data}, indent=2)
