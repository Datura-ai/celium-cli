# e2e — this checkout's CLI and SDK against a live Lium API

The renter's first hour, asserted: `balance --json` → `ls --format json` (stable fields, `--gpu` filter) → `up`
(exactly one pod) → `ps` until `RUNNING` with an `ssh_cmd` → `describe --json` → `exec` (exit code 7 comes back
as exit 7 and in the JSON; `nvidia-smi -L` lists at least the GPUs billed) → `scp` up and down, byte-exact →
billing moves while the pod runs → `rm` → gone from `ps` → the final charge fits the wall clock at the node's
price (per-second, no 15-minute floor). The error contract agents depend on: wrong key exit 3 with a JSON
error, no key exit 2 naming what to do, unknown pod exit 5 for `exec`/`describe`/`rm`, `ps` with no pods is `[]`.
Then the SDK, the way `docs/developers/sdk/examples/pod-lifecycle.md` uses it: `ls` → `up` → `wait_ready` → `exec`
(success and a non-zero exit as a dict) → `upload`/`download` → `ps` → `rm`.

These are PERSONA_TESTS' renter journeys (7 Sep 2026, 31 + 16 steps by hand on staging) as tests that run on every
PR. They rent the cheapest ≥1-GPU node under `E2E_MAX_PRICE` (default $0.50/h) for a few minutes — about $0.02 a run
on staging's A4000.

## Run it

```sh
E2E_API_KEY=<key of a funded account> ./e2e/run.sh              # staging.lium.io by default
E2E_API_URL=http://localhost:8000/api E2E_API_KEY=… ./e2e/run.sh # the lium-platform e2e stack (its seeded key)
SUITES=renter ./e2e/run.sh                                     # one journey; E2E_KEEP_POD=1 keeps the pod on failure
```

`run.sh` installs this checkout (editable) plus pytest into `e2e/.venv` (uv or pip), runs `test_renter_journey.py`
and `test_sdk_journey.py` each under a hard timeout (`T_SUITE` 25m), and always leaves `e2e/artifacts/`:
`timings.txt`, `summary.md`, `<suite>-junit.xml`, `commands.json` (every CLI call with exit code, duration and the
head of its output — the key is never written anywhere). Every `lium` call runs with `HOME` set to a temp dir, so
`up` mints its SSH key there and the first-run completion hook touches no real shell rc; the key travels only as
`LIUM_API_KEY`, the target only as `LIUM_BASE_URL`.

Without `E2E_API_KEY` every test skips and `run.sh` exits 0 — nothing to run against is not a failure.

## CI (`.github/workflows/ci.yml`, job `e2e-live`)

Runs on every PR from this repo, on `workflow_dispatch`, and once a day (`schedule`) so staging drift shows up
without a push. Needs the repository secret **`LIUM_E2E_API_KEY`** (a funded staging account; ~$1 lasts weeks) and,
optionally, the variable `LIUM_E2E_API_URL`. Fork PRs have no secrets → the job skips and stays green. One run at a
time repo-wide (`concurrency: e2e-live-staging`, no cancel): staging has one node, and two suites renting it at once
would fail each other. Artifacts uploaded on every run; `summary.md` posted as one sticky PR comment.

A stale pod from a run that died mid-way (name `e2e-…`, older than 30 min) is removed at the start of the next run.

## What it cannot see

Signup and funding (a fingerprint signup creates a junk account per run; card top-up is browser + JWT only — the
lium-platform e2e stack covers signup, Stripe test mode on staging is the only place for payments), `@lium.machine`
(covered by the serverless persona; a candidate for a third journey once the decorator ships), provider commands
(`lium mine`, `lium provider …` need a chain hotkey and a host to run an executor on — the lium-io e2e covers the
executor side).
