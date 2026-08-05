"""Signup action: create an account and keep the API key it mints."""

import os
import secrets
import string

import requests

from lium.cli.actions import ActionResult
from lium.cli.settings import config

PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*-_"
PASSWORD_LENGTH = 20
REQUEST_TIMEOUT = 30
DEFAULT_KEY_NAME = "Default"
DEFAULT_BASE_URL = "https://lium.io/api"


def generate_password() -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def base_url() -> str:
    # read at call time so LIUM_BASE_URL can point signup at staging, same as the SDK
    return os.getenv("LIUM_BASE_URL", DEFAULT_BASE_URL)


class SignupAction:
    """Register an account and return the API key minted for it.

    The account is created by ``POST /users``; the API key that call mints is
    read back with ``POST /users/login`` + ``GET /keys``. Newer backends return
    the key in the signup response itself — when they do, the two extra calls
    are skipped.
    """

    def __init__(self, email: str, password: str, name: str):
        self.email = email
        self.password = password
        self.name = name

    def execute(self, ctx: dict) -> ActionResult:
        # a stored key means an account is already wired up here — refuse rather than
        # strand the caller with a second account they cannot reach
        if config.get("api.api_key"):
            return ActionResult(
                ok=False,
                data={},
                error="An API key is already configured. Run 'lium config unset api.api_key' first, "
                      "or use 'lium init' to re-authenticate.",
            )

        created = self._create_account()
        if not created.ok:
            return created

        api_key = created.data.get("api_key") or self._read_minted_key()
        if not api_key:
            return ActionResult(
                ok=False,
                data={},
                error="Account created, but the API key could not be read back. "
                      "Log in at https://lium.io and copy the key from the dashboard.",
            )

        config.set("api.api_key", api_key)
        return ActionResult(ok=True, data={"api_key": api_key, "email": self.email})

    def _create_account(self) -> ActionResult:
        try:
            response = requests.post(
                f"{base_url()}/users",
                json={"name": self.name, "email": self.email, "password": self.password},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            return ActionResult(ok=False, data={}, error=f"Signup request failed: {e}")

        if response.status_code == 429:
            return ActionResult(
                ok=False,
                data={},
                error="Too many signups from this network. Wait and retry, or sign up at https://lium.io.",
            )

        if response.status_code >= 400:
            return ActionResult(ok=False, data={}, error=self._describe_failure(response))

        try:
            body = response.json()
        except ValueError:
            body = {}
        return ActionResult(ok=True, data={"api_key": body.get("api_key")})

    def _read_minted_key(self) -> str | None:
        try:
            login = requests.post(
                f"{base_url()}/users/login",
                json={"email": self.email, "password": self.password},
                timeout=REQUEST_TIMEOUT,
            )
            login.raise_for_status()
            token = login.json().get("token")
            if not token:
                return None

            keys = requests.get(
                f"{base_url()}/keys",
                headers={"Authorization": f"Bearer {token}"},
                timeout=REQUEST_TIMEOUT,
            )
            keys.raise_for_status()
        except (requests.RequestException, ValueError):
            return None

        entries = keys.json()
        if not isinstance(entries, list) or not entries:
            return None

        default = next((e for e in entries if e.get("name") == DEFAULT_KEY_NAME), entries[0])
        return default.get("key")

    @staticmethod
    def _describe_failure(response: requests.Response) -> str:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None

        if isinstance(detail, dict):
            detail = "; ".join(f"{k}: {v}" for k, v in detail.items())
        return str(detail) if detail else f"Signup failed with HTTP {response.status_code}."
