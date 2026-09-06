"""Opt-in crash reporting (DAH-2057).

Off unless the user turns it on: ``LIUM_TELEMETRY=1`` in the environment, or
``lium config set telemetry.enabled true``. When on, an *unexpected* exception in a
command (the ``Unexpected error:`` branch of ``handle_errors``, never an API or
usage error) is sent to Sentry with: the exception type and message, its stack
frames (file, line, function), the command name (``lium up``), the CLI version,
the Python version and the OS. Never sent: arguments, option values, local
variables, pod names, hosts, paths under the home directory, e-mails, API keys.

Without a project DSN this module does nothing even when enabled — the CLI ships
with ``DEFAULT_SENTRY_DSN`` blank until the Lium CLI Sentry project exists;
``LIUM_SENTRY_DSN`` overrides it (self-hosted GlitchTip, testing).
"""

import os
import platform
import re
from typing import Any, Optional

import click

# the Lium CLI project's public DSN; blank = no reporting even when the user opted in
DEFAULT_SENTRY_DSN = ""

_TRUE = {"1", "true", "yes", "on"}

# home directories (a username is PII) and the usual credential shapes
_SCRUB = (
    (re.compile(r"(?:/Users|/home)/[^/\s'\"]+"), "~"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"), "[email]"),
    (re.compile(r"\bsk_[A-Za-z0-9_-]{16,}"), "[api-key]"),
    (re.compile(r"\b(?:ssh-(?:rsa|dss|ed25519)|ecdsa-sha2-nistp\d{3})\s+[A-Za-z0-9+/=]+(?:\s+\S+)?"), "[ssh-key]"),
)

_initialised = False


def enabled() -> bool:
    """The user's choice: LIUM_TELEMETRY wins, then telemetry.enabled in ~/.lium/config.ini."""
    env = os.environ.get("LIUM_TELEMETRY")
    if env is not None:
        return env.strip().lower() in _TRUE
    try:
        from .settings import ConfigManager

        value = ConfigManager().get("telemetry.enabled")
    except Exception:
        return False
    return bool(value) and value.strip().lower() in _TRUE


def dsn() -> str:
    return os.environ.get("LIUM_SENTRY_DSN") or DEFAULT_SENTRY_DSN


def scrub_text(text: str) -> str:
    for pattern, placeholder in _SCRUB:
        text = pattern.sub(placeholder, text)
    return text


def _scrub_event(event: dict, hint: Any) -> Optional[dict]:
    # nothing about the machine or the session beyond what init() tagged
    for key in ("request", "user", "breadcrumbs", "server_name", "modules", "extra"):
        event.pop(key, None)
    for value in (event.get("exception") or {}).get("values") or []:
        if isinstance(value.get("value"), str):
            value["value"] = scrub_text(value["value"])
        for frame in ((value.get("stacktrace") or {}).get("frames") or []):
            frame.pop("vars", None)
            for path_key in ("abs_path", "filename"):
                if isinstance(frame.get(path_key), str):
                    frame[path_key] = scrub_text(frame[path_key])
    if isinstance(event.get("message"), str):
        event["message"] = scrub_text(event["message"])
    return event


def init(command: Optional[str], version: str) -> bool:
    """Start the SDK for this invocation. False when off, or when there is no DSN."""
    global _initialised
    if _initialised:
        return True
    if not enabled() or not dsn():
        return False
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn(),
        release=f"lium-cli@{version}",
        # explicit capture only: no excepthook, no logging or HTTP breadcrumbs, no module list
        default_integrations=False,
        max_breadcrumbs=0,
        include_local_variables=False,
        send_default_pii=False,
        traces_sample_rate=0,
        before_send=_scrub_event,
    )
    sentry_sdk.set_tag("command", command or "lium")
    sentry_sdk.set_tag("python", platform.python_version())
    sentry_sdk.set_tag("os", platform.system().lower())
    _initialised = True
    return True


def report(exc: BaseException) -> bool:
    """Send one unexpected exception; waits up to two seconds so the process may exit right after."""
    if not _initialised:
        return False
    import sentry_sdk

    context = click.get_current_context(silent=True)
    if context is not None:
        sentry_sdk.set_tag("command", context.command_path)
    sentry_sdk.capture_exception(exc)
    sentry_sdk.flush(timeout=2)
    return True


OPT_IN_HINT = "Report crashes like this automatically: lium config set telemetry.enabled true"
