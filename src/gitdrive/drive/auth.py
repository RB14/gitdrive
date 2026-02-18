"""OAuth2 authentication manager with encrypted token storage."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
from cryptography.fernet import Fernet, InvalidToken
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from gitdrive.config import GitDriveConfig
from gitdrive.exceptions import AuthenticationError


class AuthManager:
    """Manages OAuth2 authentication for Google Drive API access.

    Handles the full lifecycle: guiding users through credential setup,
    running the OAuth2 consent flow, and storing / loading encrypted tokens.
    """

    SCOPES = ["https://www.googleapis.com/auth/drive.file"]

    def __init__(self, config: GitDriveConfig) -> None:
        self._config = config

    # ── Public API ───────────────────────────────────────────────

    def setup_credentials(self) -> None:
        """Semi-automated guide for setting up OAuth2 credentials.

        Prints step-by-step instructions for the Google Cloud Console and
        prompts the user for the path to the downloaded *credentials.json*.
        The file is validated and copied into the config directory.
        """
        click.echo(
            "\n"
            "╭─ Google Drive API — Credential Setup ──────────────────────╮\n"
            "│                                                            │\n"
            "│  1. Go to https://console.cloud.google.com                 │\n"
            "│  2. Create a new project (or select an existing one)       │\n"
            "│  3. Navigate to APIs & Services > Library                  │\n"
            "│  4. Search for 'Google Drive API' and enable it            │\n"
            "│  5. Go to APIs & Services > OAuth consent screen           │\n"
            "│     - Configure the consent screen if not done yet         │\n"
            "│     - Set publishing status to 'In production' to avoid    │\n"
            "│       token expiry every 7 days (safe for personal use)    │\n"
            "│     - Or: stay in 'Testing' and add your Google email      │\n"
            "│       under 'Test users' (tokens expire weekly)            │\n"
            "│  6. Go to APIs & Services > Credentials                    │\n"
            "│  7. Click 'Create Credentials' > 'OAuth client ID'         │\n"
            "│  8. Choose application type: 'Desktop app'                 │\n"
            "│  9. Download the JSON file                                 │\n"
            "│                                                            │\n"
            "╰────────────────────────────────────────────────────────────╯\n"
        )

        path_str = click.prompt(
            "Enter the path to the downloaded credentials JSON file",
            type=str,
        )
        source = Path(path_str).expanduser().resolve()

        if not source.is_file():
            raise AuthenticationError(f"File not found: {source}")

        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise AuthenticationError(
                f"Failed to read credentials file: {exc}"
            ) from exc

        installed = data.get("installed")
        if not isinstance(installed, dict):
            raise AuthenticationError(
                "Invalid credentials file: missing 'installed' key. "
                "Make sure you downloaded an OAuth 2.0 Desktop client ID."
            )

        for required_key in ("client_id", "client_secret"):
            if required_key not in installed:
                raise AuthenticationError(
                    f"Invalid credentials file: missing '{required_key}' "
                    f"inside 'installed' object."
                )

        dest = self._config.credentials_file
        shutil.copy2(source, dest)
        dest.chmod(0o600)
        click.echo(f"\nCredentials saved to {dest}")

    def login(self) -> None:
        """Run the OAuth2 flow and store an encrypted token.

        Uses :pyclass:`InstalledAppFlow` with a local server on an
        auto-selected port.  The resulting credentials are encrypted with
        Fernet and persisted to disk.

        Raises:
            AuthenticationError: If *credentials.json* is not found.
        """
        creds_file = self._config.credentials_file
        if not creds_file.is_file():
            raise AuthenticationError(
                f"Credentials file not found at {creds_file}. "
                "Run 'gitdrive auth setup' first."
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(creds_file),
            scopes=self.SCOPES,
        )
        creds = flow.run_local_server(port=0)
        self._encrypt_credentials(creds)
        click.echo("Login successful — token encrypted and stored.")

    def get_credentials(self) -> Credentials:
        """Load, decrypt, and return valid credentials.

        If the token is expired but a refresh token is available, it is
        refreshed automatically and re-encrypted.

        Raises:
            AuthenticationError: On missing files or failed refresh.
        """
        creds = self._decrypt_credentials()

        if creds.valid:
            return creds

        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise AuthenticationError(
                    f"Token refresh failed: {exc}"
                ) from exc
            self._encrypt_credentials(creds)
            return creds

        raise AuthenticationError(
            "Stored token is invalid and cannot be refreshed. "
            "Run 'gitdrive auth login' to re-authenticate."
        )

    def get_status(self) -> dict[str, Any]:
        """Return a dictionary describing the current authentication state.

        Keys:
            credentials_configured (bool): ``True`` when *credentials.json* exists.
            token_valid (bool): ``True`` when a token can be decrypted and is
                not expired (or can be refreshed).
            token_expiry (str | None): ISO 8601 expiry timestamp, or ``None``.
        """
        status: dict[str, Any] = {
            "credentials_configured": self._config.credentials_file.is_file(),
            "token_valid": False,
            "token_expiry": None,
        }

        try:
            creds = self.get_credentials()
            status["token_valid"] = True
            if creds.expiry is not None:
                status["token_expiry"] = creds.expiry.isoformat()
        except AuthenticationError:
            # Token is missing, corrupt, or cannot be refreshed.
            pass

        return status

    # ── Private helpers ──────────────────────────────────────────

    def _encrypt_credentials(self, creds: Credentials) -> None:
        """Serialize, encrypt, and write *creds* to disk."""
        key = self._get_or_create_key()
        fernet = Fernet(key)

        payload: dict[str, Any] = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes) if creds.scopes else None,
            "expiry": (
                creds.expiry.isoformat() if creds.expiry is not None else None
            ),
        }

        plaintext = json.dumps(payload).encode("utf-8")
        ciphertext = fernet.encrypt(plaintext)

        token_path = self._config.token_file
        token_path.write_bytes(ciphertext)
        token_path.chmod(0o600)

    def _decrypt_credentials(self) -> Credentials:
        """Read, decrypt, and deserialize credentials from disk.

        Raises:
            AuthenticationError: If key or token file is missing or
                decryption fails.
        """
        key_path = self._config.encryption_key_file
        token_path = self._config.token_file

        if not key_path.is_file():
            raise AuthenticationError(
                f"Encryption key not found at {key_path}. "
                "Run 'gitdrive auth login' first."
            )
        if not token_path.is_file():
            raise AuthenticationError(
                f"Token file not found at {token_path}. "
                "Run 'gitdrive auth login' first."
            )

        try:
            key = key_path.read_bytes().strip()
            fernet = Fernet(key)
            plaintext = fernet.decrypt(token_path.read_bytes())
        except (InvalidToken, ValueError, OSError) as exc:
            raise AuthenticationError(
                f"Failed to decrypt token: {exc}"
            ) from exc

        data: dict[str, Any] = json.loads(plaintext.decode("utf-8"))

        expiry = None
        if data.get("expiry") is not None:
            expiry = datetime.fromisoformat(data["expiry"])
            # Google's auth library uses naive UTC datetimes internally,
            # so strip any timezone info to avoid comparison errors.
            if expiry.tzinfo is not None:
                expiry = expiry.replace(tzinfo=None)

        return Credentials(
            token=data["token"],
            refresh_token=data.get("refresh_token"),
            token_uri=data.get("token_uri"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=data.get("scopes"),
            expiry=expiry,
        )

    def _get_or_create_key(self) -> bytes:
        """Load an existing Fernet key or generate and persist a new one."""
        key_path = self._config.encryption_key_file

        if key_path.is_file():
            return key_path.read_bytes().strip()

        key = Fernet.generate_key()
        key_path.write_bytes(key + b"\n")
        key_path.chmod(0o600)
        return key
