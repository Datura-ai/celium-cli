"""Opt-in crash reporting (DAH-2057).

Off unless the user turns it on: ``LIUM_TELEMETRY=1`` in the environment, or
``lium config set telemetry.enabled true``. When on, an *unexpected* exception in a
command (the ``Unexpected error:`` branch of ``handle_errors``, never an API or
usage error) is sent to Sentry with: the exception type and message, its stack
frames (file, line, function), the command name (``lium up``), the CLI version,
the Python version and the OS. Never sent: arguments, option values, local
variables, pod names, hosts, paths under the home directory, e-mails, API keys.

``DEFAULT_SENTRY_DSN`` is the Lium CLI project's public client key (org datura-gc,
project lium-cli — DAH-3121). A DSN only lets a client *send* events to that
project; it reads nothing. ``LIUM_SENTRY_DSN`` overrides it (self-hosted GlitchTip,
testing); ``LIUM_SENTRY_DSN=`` (empty) disables reporting even when opted in.

The Sentry ``environment`` follows the API the CLI talks to (``LIUM_BASE_URL``):
``prod`` for lium.io (the name the platform stacks report too — Pulumi stack ``prod`` — so one
``environment:prod`` search covers backend, web and CLI), ``staging`` for the staging host, ``dev``
otherwise — so a crash against a dev stack never counts as a production issue.
"""

import os
import platform
import re
from typing import Any, Optional

import click

# the Lium CLI project's public DSN (datura-gc / lium-cli); a client key, not a secret
DEFAULT_SENTRY_DSN = "https://cc6f063f312389b480fdbc5b473443cc@o4508882177228800.ingest.de.sentry.io/4512042277601360"

DEFAULT_API_HOST = "lium.io"

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
    override = os.environ.get("LIUM_SENTRY_DSN")
    if override is not None:
        return override.strip()
    return DEFAULT_SENTRY_DSN


def api_host() -> str:
    """Host part of the API the CLI is configured for (LIUM_BASE_URL or the default)."""
    from urllib.parse import urlsplit

    base = os.environ.get("LIUM_BASE_URL") or ""
    try:
        host = urlsplit(base).hostname if base else None
    except ValueError:
        host = None
    return host or DEFAULT_API_HOST


def environment(host: Optional[str] = None) -> str:
    """prod for lium.io (the platform stack name), staging for a staging host, dev for anything else."""
    host = (host or api_host()).lower()
    if host in {DEFAULT_API_HOST, f"www.{DEFAULT_API_HOST}", f"api.{DEFAULT_API_HOST}"}:
        return "prod"
    if "staging" in host:
        return "staging"
    return "dev"


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

    host = api_host()
    sentry_sdk.init(
        dsn=dsn(),
        release=f"lium-cli@{version}",
        environment=environment(host),
        # explicit capture only: no excepthook, no logging or HTTP breadcrumbs, no module list
        default_integrations=False,
        max_breadcrumbs=0,
        include_local_variables=False,
        send_default_pii=False,
        max_request_body_size="never",   # the CLI makes requests but never serves them; nothing request-shaped may travel
        traces_sample_rate=0,
        before_send=_scrub_event,
    )
    sentry_sdk.set_tag("command", command or "lium")
    sentry_sdk.set_tag("cli_version", version)
    sentry_sdk.set_tag("api_host", host)
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
    # the exception class is what a triager filters on (KeyError vs ConnectionError); grouping
    # itself stays Sentry's stack-trace grouping — a CLI crash is one bug at one place
    sentry_sdk.set_tag("error_class", type(exc).__name__)
    sentry_sdk.capture_exception(exc)
    sentry_sdk.flush(timeout=2)
    return True


OPT_IN_HINT = "Report crashes like this automatically: lium config set telemetry.enabled true"
