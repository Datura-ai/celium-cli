"""Signup actions: create an account — by email or by Bittensor key — and keep the API key it mints."""

import os
import secrets
import string
from datetime import datetime, timezone

import requests

from lium.cli.actions import ActionResult
from lium.cli.settings import config
from lium.provider.errors import ProviderError
from lium.provider.wallet import load_hotkey_keypair

PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*-_"
PASSWORD_LENGTH = 20
REQUEST_TIMEOUT = 30
MINTED_KEY_NAME = "Default"
DEFAULT_BASE_URL = "https://lium.io/api"
RATE_LIMIT_MESSAGE = "Too many signups from this network. Wait and retry, or sign up at https://lium.io."


def generate_password() -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def base_url() -> str:
    # read at call time so LIUM_BASE_URL can point signup at staging, same as the SDK
    return os.getenv("LIUM_BASE_URL", DEFAULT_BASE_URL)


def _json_object(response: requests.Response) -> dict:
    # a proxy between us and the backend can answer with valid JSON that is not an object
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _is_expired(expires_at) -> bool:
    # the backend stores naive UTC timestamps, so an offset-aware value is normalised before comparing
    if not isinstance(expires_at, str):
        return False
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    if expiry.tzinfo is not None:
        expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
    return expiry <= datetime.now(timezone.utc).replace(tzinfo=None)


def _select_minted_key(api_keys: list) -> str | None:
    # GET /keys lists dead keys too — a restored account can carry several rows named "Default"
    usable = [
        k for k in api_keys
        if isinstance(k, dict)
        and k.get("key")
        and k.get("is_active", True)
        and not _is_expired(k.get("expires_at"))
    ]
    if not usable:
        return None

    newest = max(usable, key=lambda k: (k.get("name") == MINTED_KEY_NAME, str(k.get("created_at") or "")))
    return newest.get("key")


def _configured_key_refusal() -> ActionResult | None:
    # a second account would be unreachable — nothing here can switch between keys
    if not config.get("api.api_key"):
        return None

    if os.environ.get("LIUM_API_KEY"):
        return ActionResult(
            ok=False,
            data={},
            error="An API key is already configured through the LIUM_API_KEY environment "
                  "variable. Run 'unset LIUM_API_KEY' first, or use 'lium init' to "
                  "re-authenticate.",
        )
    return ActionResult(
        ok=False,
        data={},
        error="An API key is already configured. Run 'lium config unset api.api_key' first, "
              "or use 'lium init' to re-authenticate.",
    )


def _describe_failure(response: requests.Response, action: str = "Signup") -> str:
    # the backend names the offending field in a detail dict; older routes answer with a bare string
    detail = _json_object(response).get("detail")

    if isinstance(detail, dict):
        detail = "; ".join(f"{k}: {v}" for k, v in detail.items())
    return str(detail) if detail else f"{action} failed with HTTP {response.status_code}."


class SignupAction:
    """Register an account and return the API key minted for it.

    The account is created by ``POST /users``; the API key that call mints is
    read back with ``POST /users/login`` + ``GET /keys``. Newer backends return
    the key in the signup response itself — when they do, the two extra calls
    are skipped.
    """

    def __init__(self, email: str, password: str, display_name: str):
        self.email = email
        self.password = password
        self.display_name = display_name

    def execute(self, ctx: dict) -> ActionResult:
        refusal = _configured_key_refusal()
        if refusal:
            return refusal

        creation_result = self._create_account()
        if not creation_result.ok:
            return creation_result

        api_key = creation_result.data.get("api_key") or self._read_minted_key()
        if not api_key:
            return ActionResult(
                ok=False,
                data={"account_may_exist": True},
                error="Account created, but the API key could not be read back.",
            )

        config.set("api.api_key", api_key)
        return ActionResult(ok=True, data={
            "api_key": api_key,
            "signup_credit_granted": creation_result.data.get("signup_credit_granted"),
        })

    def _create_account(self) -> ActionResult:
        try:
            response = requests.post(
                f"{base_url()}/users",
                json={"name": self.display_name, "email": self.email, "password": self.password},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            # the account is created and its mail sent inside the call, so any transport failure
            # after the request left can still leave an account behind
            return ActionResult(
                ok=False,
                data={"account_may_exist": True},
                error=f"Signup request failed: {e}. The account may have been created.",
            )

        if response.status_code == 429:
            return ActionResult(ok=False, data={}, error=RATE_LIMIT_MESSAGE)

        if response.status_code >= 400:
            return ActionResult(ok=False, data={}, error=_describe_failure(response))

        body = _json_object(response)
        return ActionResult(ok=True, data={
            "api_key": body.get("api_key"),
            "signup_credit_granted": body.get("signup_credit_granted"),
        })

    def _read_minted_key(self) -> str | None:
        try:
            login_response = requests.post(
                f"{base_url()}/users/login",
                json={"email": self.email, "password": self.password},
                timeout=REQUEST_TIMEOUT,
            )
            login_response.raise_for_status()
            token = login_response.json().get("token")
            if not token:
                return None

            keys_response = requests.get(
                f"{base_url()}/keys",
                headers={"Authorization": f"Bearer {token}"},
                timeout=REQUEST_TIMEOUT,
            )
            keys_response.raise_for_status()
            api_keys = keys_response.json()
        except (requests.RequestException, ValueError):
            return None

        if not isinstance(api_keys, list):
            return None

        return _select_minted_key(api_keys)


class KeySignupAction:
    """Register an account owned by a Bittensor hotkey and return the API key minted for it.

    ``POST /auth/wallet-challenge`` mints a single-use message, the hotkey signs
    it, and ``POST /auth/wallet-signup`` trades the signature for an account
    plus its default API key. No email and no password are involved — attach
    them later with :class:`AttachEmailAction`.
    """

    def __init__(self, coldkey: str, hotkey: str, display_name: str | None):
        self.coldkey = coldkey
        self.hotkey = hotkey
        self.display_name = display_name

    def execute(self, ctx: dict) -> ActionResult:
        refusal = _configured_key_refusal()
        if refusal:
            return refusal

        try:
            keypair = load_hotkey_keypair(self.coldkey, self.hotkey)
        except ProviderError as e:
            return ActionResult(ok=False, data={}, error=str(e))

        address = keypair.ss58_address
        challenge_result = self._request_challenge(address)
        if not challenge_result.ok:
            return challenge_result

        # the backend verifies the signature over the message it minted, as bare hex without 0x
        signature = keypair.sign(challenge_result.data["message"]).hex()

        signup_result = self._register(address, challenge_result.data["nonce"], signature)
        if not signup_result.ok:
            return signup_result

        api_key = signup_result.data.get("api_key")
        if not api_key:
            return ActionResult(
                ok=False,
                data={},
                error=f"Account created for {address}, but the API key could not be read back. "
                      "Copy it from https://lium.io.",
            )

        config.set("api.api_key", api_key)
        return ActionResult(ok=True, data={
            "address": address,
            "api_key": api_key,
            "is_new_user": signup_result.data.get("is_new_user"),
            "signup_credit_granted": signup_result.data.get("signup_credit_granted"),
        })

    def _request_challenge(self, address: str) -> ActionResult:
        # the challenge is single-use and expires in 300 s, so it is minted right before signing
        try:
            response = requests.post(
                f"{base_url()}/auth/wallet-challenge",
                json={"address": address},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            return ActionResult(ok=False, data={}, error=f"Wallet challenge request failed: {e}.")

        if response.status_code >= 400:
            return ActionResult(ok=False, data={}, error=_describe_failure(response, "Wallet challenge"))

        body = _json_object(response)
        if not body.get("nonce") or not body.get("message"):
            return ActionResult(
                ok=False,
                data={},
                error="The wallet challenge response carried no message to sign.",
            )
        return ActionResult(ok=True, data={"nonce": body["nonce"], "message": body["message"]})

    def _register(self, address: str, nonce: str, signature: str) -> ActionResult:
        # the account is created inside this call, so a transport failure can still leave one behind
        payload = {"address": address, "nonce": nonce, "signature": signature}
        if self.display_name:
            payload["name"] = self.display_name

        try:
            response = requests.post(
                f"{base_url()}/auth/wallet-signup",
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            return ActionResult(
                ok=False,
                data={},
                error=f"Wallet signup request failed: {e}. The account may have been created — "
                      "check https://lium.io before retrying.",
            )

        if response.status_code == 429:
            return ActionResult(ok=False, data={}, error=RATE_LIMIT_MESSAGE)

        if response.status_code >= 400:
            return ActionResult(ok=False, data={}, error=_describe_failure(response))

        body = _json_object(response)
        return ActionResult(ok=True, data={
            "api_key": body.get("api_key"),
            "is_new_user": body.get("is_new_user"),
            "signup_credit_granted": body.get("signup_credit_granted"),
        })


class AttachEmailAction:
    """Attach an email + password login to the account the stored API key belongs to.

    The account keeps its key-only owner; the email becomes a second way in and
    stays unverified until the user clicks the link the backend mails.
    """

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password

    def execute(self, ctx: dict) -> ActionResult:
        api_key = config.get("api.api_key")
        if not api_key:
            return ActionResult(
                ok=False,
                data={},
                error="No API key is configured. Run 'lium signup-key --coldkey <name> --hotkey <name>' "
                      "first, or 'lium init' to authenticate an existing account.",
            )

        try:
            response = requests.post(
                f"{base_url()}/users/me/email-password",
                json={"email": self.email, "password": self.password},
                headers={"X-API-Key": api_key},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            # the verification mail is sent inside the call, so a transport failure can still leave it attached
            return ActionResult(
                ok=False,
                data={"credential_may_exist": True},
                error=f"Attaching the email failed: {e}. It may have been attached.",
            )

        if response.status_code >= 400:
            return ActionResult(ok=False, data={}, error=_describe_failure(response, "Attaching the email"))

        return ActionResult(ok=True, data={})
