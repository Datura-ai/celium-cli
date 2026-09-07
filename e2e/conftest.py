"""The real `lium` CLI of this checkout against a real Lium API, from a throwaway HOME.

Target (env):
  E2E_API_URL   the API the CLI talks to — default https://staging.lium.io/api; the lium-platform e2e stack works too
                (http://localhost:8000/api with its seeded key).
  E2E_API_KEY   an API key of a FUNDED account there (the rent tests skip, loudly, on a zero balance).
  E2E_LIUM      the lium executable (default: `lium` on PATH — CI installs this checkout into a venv first).
  E2E_MAX_PRICE the most the suite will rent per hour (default 0.50 $/h); the cheapest listed node is chosen.
  E2E_KEEP_POD  =1 leaves the pod up on failure for a human to look at (never in CI).

Every command runs with HOME set to a temp dir, so `up` mints its SSH key there and the first-run shell-completion
hook edits a shell rc nobody uses (L-65: never the runner's ~/.lium). The key travels only as LIUM_API_KEY.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

API_URL = os.environ.get("E2E_API_URL", "https://staging.lium.io/api").rstrip("/")
API_KEY = os.environ.get("E2E_API_KEY", "")
LIUM = os.environ.get("E2E_LIUM", "lium")
MAX_PRICE = float(os.environ.get("E2E_MAX_PRICE", "0.50"))
KEEP_POD = os.environ.get("E2E_KEEP_POD", "") == "1"
ARTIFACTS = Path(os.environ.get("E2E_ARTIFACTS", Path(__file__).parent / "artifacts"))


@dataclass
class Result:
    argv: list[str]
    rc: int
    out: str
    err: str
    seconds: float

    def json(self):
        return json.loads(self.out)

    def __repr__(self) -> str:  # short, key-free
        return f"<lium {' '.join(self.argv)} rc={self.rc} {self.seconds:.1f}s out={self.out[:200]!r} err={self.err[:200]!r}>"


@dataclass
class Session:
    home: Path
    log: list[dict] = field(default_factory=list)

    def lium(self, *args: str, key: str | None = API_KEY, timeout: int = 180, check: bool = False) -> Result:
        env = {
            "HOME": str(self.home),
            "PATH": os.environ["PATH"],
            "LIUM_BASE_URL": API_URL,
            "TERM": "dumb",
            "NO_COLOR": "1",
            "SHELL": "/bin/sh",
        }
        if key is not None:
            env["LIUM_API_KEY"] = key
        for passthrough in ("VIRTUAL_ENV", "PYTHONPATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
            if passthrough in os.environ:
                env[passthrough] = os.environ[passthrough]
        t0 = time.monotonic()
        p = subprocess.run([LIUM, *args], env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        r = Result(list(args), p.returncode, p.stdout, p.stderr, time.monotonic() - t0)
        self.log.append({"argv": r.argv, "rc": r.rc, "seconds": round(r.seconds, 2), "stdout_head": r.out[:400], "stderr_head": r.err[:400]})
        if check and r.rc != 0:
            raise AssertionError(f"expected exit 0: {r!r}")
        return r


def _write_artifacts(session: Session) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "commands.json").write_text(json.dumps(session.log, indent=1))


@pytest.fixture(scope="session")
def session(tmp_path_factory) -> Session:
    if not API_KEY:
        pytest.skip("E2E_API_KEY is not set — nothing to run against (set E2E_API_URL/E2E_API_KEY, see e2e/README.md)")
    if shutil.which(LIUM) is None and not Path(LIUM).exists():
        pytest.fail(f"lium executable not found: {LIUM} (install this checkout, or set E2E_LIUM)")
    home = tmp_path_factory.mktemp("home")
    (home / ".ssh").mkdir(mode=0o700)
    s = Session(home=home)
    yield s
    _write_artifacts(s)


@dataclass
class Rental:
    """One pod rented for the suite, removed no matter what (fixture finalizer + a sweep of stale e2e- pods)."""

    name: str
    executor_id: str = ""
    price_per_hour: float = 0.0
    gpu_type: str = ""
    pod: dict = field(default_factory=dict)
    up_called_at: float = 0.0
    running_at: float = 0.0
    balance_before: float | None = None


def ps(session: Session) -> list[dict]:
    r = session.lium("ps", "--format", "json", check=True)
    return r.json()


def rm_pods_named(session: Session, prefix: str, older_than_s: float = 0) -> int:
    n = 0
    now = time.time()
    for p in ps(session):
        name = p.get("name") or ""
        if not name.startswith(prefix):
            continue
        created = p.get("created_at")
        if older_than_s and created:
            try:
                from datetime import datetime, timezone

                age = now - datetime.fromisoformat(str(created).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
                if age < older_than_s:
                    continue
            except ValueError:
                pass
        session.lium("rm", name, "-y", timeout=120)
        n += 1
    return n


@pytest.fixture(scope="session")
def rental(session: Session) -> Rental:
    # a previous run that died mid-way may have left a pod; anything of ours older than 30 min goes first
    swept = rm_pods_named(session, "e2e-", older_than_s=30 * 60)
    if swept:
        session.log.append({"note": f"swept {swept} stale e2e- pod(s)"})
    r = Rental(name=f"e2e-{time.strftime('%H%M%S')}-{os.getpid() % 1000:03d}")
    yield r
    if r.pod and not KEEP_POD:
        for _ in range(3):
            res = session.lium("rm", r.name, "-y", timeout=120)
            if res.rc == 0 or not any(p.get("name") == r.name for p in ps(session)):
                break
            time.sleep(5)
