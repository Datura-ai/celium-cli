# Lium for agents and scripts

One page for an LLM agent (or any unattended script) that has to rent a GPU pod, run work on it, collect the results and give the pod back, without a human at the keyboard. Everything here is a shell command plus `jq`, or the Python SDK; nothing needs a terminal.

The rules of the road:

1. **Authenticate from the environment.** `LIUM_API_KEY` wins over `~/.lium/config.ini`. Never run `lium init` from an agent.
2. **Ask for JSON.** `--format json` (or `--json`) puts the result on stdout; errors go to stderr as one JSON object; the exit code says whether it worked.
3. **Never let a command wait for a human.** Pass `--yes` to anything that would confirm, and set `LIUM_NONINTERACTIVE=1` so a forgotten prompt fails fast instead of hanging.
4. **Always give a pod a lifetime** (`--ttl`) and always remove it when finished, including on failure.

## Which flags exist where

The Lium repository is moving quickly. Flags below marked *main* are on `origin/main`; the others live on a pushed branch and become available when that branch merges. Check `lium <command> --help` if in doubt.

| Capability | Flag / API | Where |
|---|---|---|
| Machine-readable listings | `lium ps --format json`, `lium ls --format json` | main |
| Machine-readable command result | `lium exec ... --json`, `lium describe --json`, `lium balance --json` | main |
| `--json` alias, `templates --format json` | `lium templates --format json`, `--json` on `ls`/`ps`/`balance` | `cli/json-output-everywhere` |
| Skip confirmations | `lium up --yes`, `lium rm --yes` (`lium reboot` never asks) | main |
| Do not open an SSH session after `up` | `lium up --no-ssh` | main |
| Auto-terminate | `lium up --ttl 6h` / `--until "18:00"` | main |
| Refuse to prompt when no TTY | `LIUM_NONINTERACTIVE=1` (or a non-TTY stdin) | `sdk/noninteractive-safety` |
| JSON errors without `--format json` | `LIUM_OUTPUT=json`; `hint` and `exit_code` fields; `docs/exit-codes.md` | `sdk/structured-errors` |
| Background command | `lium exec <pod> -d "cmd"` → `{"pid", "log"}`; `--log PATH` | `cli/exec-detach` |
| Verify GPU count after `up` | `lium up --verify-gpus`, `--strict-gpus` | `cli/up-verify-gpu-count` |
| GPU utilisation sample | `lium top <pod> [--watch N] [--all] --format json` | `sdk/top` |
| Throttled/filtered rsync, pod-to-pod copy | `lium rsync --bwlimit/--exclude/--delete`, `lium cp A:/p B:/p` | `sdk/transfers` |
| Who am I / is everything wired | `lium whoami --json`, `lium doctor --json` | `sdk/whoami-doctor` |
| JSON Schema of the objects the CLI prints | `lium schema PodInfo ExecutorInfo Template` | `sdk/schema-command` |
| SDK: `up(wait=True)`, `rent()` context manager, `exec(timeout=, detach=)`, `gpu_stats()`, `to_dict()` | Python | `sdk/ergonomics-lifecycle` |
| SDK: `LiumInsufficientBalanceError` with `.required` / `.available` | Python | `sdk/insufficient-balance-error` |

## 1. Authentication

```bash
export LIUM_API_KEY="..."          # from the Lium dashboard; takes precedence over the config file
export LIUM_NONINTERACTIVE=1       # any prompt becomes an error with a hint (sdk/noninteractive-safety)
export LIUM_OUTPUT=json            # every error is a JSON envelope on stderr (sdk/structured-errors)
lium balance --json                # cheapest possible "am I authenticated" check
```

SSH: the CLI and SDK use the first of `~/.ssh/id_ed25519`, `~/.ssh/id_rsa`, `~/.ssh/id_ecdsa` that exists. Its public key must be registered on the account (`lium up` registers it on first use; `lium whoami` on `sdk/whoami-doctor` tells you whether it is). Generate one first on a fresh machine:

```bash
[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -q -t ed25519 -N '' -f ~/.ssh/id_ed25519
```

## 2. Reading results and errors

Success: the JSON result is on **stdout**, exit code 0.

Failure: stdout is empty, **stderr** holds one JSON object, the exit code is non-zero:

```json
{"ok": false, "error": {"code": "pod_not_found", "message": "No pods match targets: train-1", "hint": "Run 'lium ps' to list pods"}, "exit_code": 5}
```

`hint` and `exit_code` arrive with `sdk/structured-errors`; `ok`, `code` and `message` are on main.

Exit codes (stable; full table in `docs/exit-codes.md` once merged):

| Code | Meaning | Typical reaction |
|---|---|---|
| 0 | success | continue |
| 1 | unclassified failure | inspect `error.message`, usually retry once |
| 2 | bad arguments or configuration (also: a prompt was needed and no terminal was available) | fix the invocation; do not retry as-is |
| 3 | the API refused or failed | back off and retry; give up after a few attempts |
| 4 | SSH could not connect | pod still booting: wait and retry |
| 5 | pod not found | re-list with `lium ps` |
| 6 | permission denied, including insufficient balance | stop; `lium balance --json` |

Pattern in a script:

```bash
set -o pipefail
if ! out=$(lium exec "$POD" --json "nvidia-smi -L" 2>err.json); then
  code=$(jq -r .error.code err.json); hint=$(jq -r '.error.hint // empty' err.json)
  ...
fi
```

`lium exec --json` exits non-zero when the remote command did; the remote `exit_code`, `stdout` and `stderr` are in `results[]`.

## 3. The lifecycle, end to end

### Pick a node

```bash
lium ls --gpu H100 --count 1 --format json | jq -r '.[0].huid'
```

`ls` output is sorted best-first (the same order as the table); each entry carries `huid`, `gpu_type`, `gpu_count`, `price_per_hour`, `vram_gb`, `disk_gb`, `max_cuda_version`, `country`. Index numbers (`1`, `2`) refer to the *last* `lium ls` run in this shell's config directory; a fresh agent should use the `huid`.

### Rent it

`lium up` has no JSON output on main, so name the pod yourself and read it back from `lium ps`:

```bash
NAME="job-$(date +%s)"
lium up "$NODE" --name "$NAME" --ttl 4h --yes --no-ssh
POD=$(lium ps --format json | jq -r --arg n "$NAME" '.[] | select(.name==$n) | .huid')
```

`--yes` skips the price confirmation, `--no-ssh` returns instead of opening a shell, `--ttl` is the safety net. `up` waits until SSH is reachable before returning. With `cli/up-verify-gpu-count` add `--verify-gpus` to have the billed GPU count checked against `nvidia-smi` inside the pod.

In Python (`sdk/ergonomics-lifecycle`), the context manager removes the pod even when your code raises:

```python
from lium.sdk import Lium
lium = Lium()
with lium.rent(executor_id=node_id, pod_name=name, wait=True, timeout=600) as pod:
    lium.exec(pod, command="nvidia-smi -L", timeout=30)
```

### Run work

Foreground, with the output back as JSON:

```bash
lium exec "$POD" --json "python -c 'import torch; print(torch.cuda.device_count())'"
```

Background, so a long training run survives the end of the SSH session (`cli/exec-detach`):

```bash
lium exec "$POD" -d --log /workspace/logs/train.log "cd /workspace && python train.py" --json
#  -> {"ok": true, "results": [{"pod": "...", "pid": 4242, "log": "/workspace/logs/train.log", ...}]}
```

Without that branch, the equivalent by hand is:

```bash
lium exec "$POD" "mkdir -p /workspace/logs && nohup setsid bash -lc 'cd /workspace && python train.py' > /workspace/logs/train.log 2>&1 < /dev/null & echo \$!"
```

The `setsid`, the `< /dev/null` and the redirection all matter: without them the process is tied to the SSH session and dies when `exec` returns.

Poll it:

```bash
lium exec "$POD" --json "kill -0 $PID && echo running || echo done; tail -n 20 /workspace/logs/train.log"
```

### Watch the GPUs

```bash
lium top "$POD" --format json            # sdk/top: one sample, [{index,name,utilization_pct,memory_used_mb,...}]
lium top "$POD" --watch 30                # refresh every 30 s (text)
lium top --all --format json             # every pod in one call
```

On main, the same numbers via `exec`:

```bash
lium exec "$POD" --json "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv,noheader,nounits"
```

In Python: `lium.gpu_stats(pod)` returns `GpuStats` objects with `.to_dict()`.

A pod that shows 0 % utilisation for several minutes after the job started is usually loading data from the wrong disk (see gotchas) or has crashed: read the log before deciding.

### Move data

```bash
lium rsync "$POD" ./data /workspace/data                         # main; mirrors rsync -avz
lium rsync "$POD" ./ckpt /workspace/ckpt --bwlimit 20000 --exclude '*.tmp' --progress   # sdk/transfers
lium scp "$POD" /workspace/out/model.safetensors ./out -d        # main; single files, download with -d
lium cp "$SRC_POD":/workspace/ckpt/ "$DST_POD":/workspace/ckpt/  # sdk/transfers; never touches this machine
```

`rsync` needs `rsync` on the pod (`apt-get install -y rsync` on minimal images). With `sdk/transfers`, transfers resume where they stopped, so a failed `rsync` is simply re-run.

### Remove the pod

```bash
lium rm "$POD" --yes
```

Do this in a trap so it runs on any exit path:

```bash
trap 'lium rm "$POD" --yes >/dev/null 2>&1 || true' EXIT
```

`lium rm --all --yes` removes every pod on the account; do not use it from a shared account.

## 4. Pod gotchas

- **`/workspace` is the fast disk.** The home volume (`/root`) is an encrypted FUSE mount when volume encryption is on (the default); large sequential reads such as model weights are noticeably slower there. Keep datasets, checkpoints and caches under `/workspace`; `mkdir -p /workspace` if the image lacks it.
- **Point Hugging Face at it before the first download:** `export HF_HOME=/workspace/hf` (and `HF_HUB_ENABLE_HF_TRANSFER=1` after `pip install hf_transfer`).
- **PEP 668 on Ubuntu 24.04 images:** a bare `pip install` fails with "externally managed environment". Use `python -m venv /workspace/venv && . /workspace/venv/bin/activate`, or `export PIP_BREAK_SYSTEM_PACKAGES=1`.
- **Blackwell needs a recent PyTorch build.** B200, B300, RTX PRO 6000 and RTX 5090 are not supported by wheels built for CUDA ≤ 12.4; install a cu128 or cu130 wheel (`pip install torch --index-url https://download.pytorch.org/whl/cu130`). A template built on cu126 on a Blackwell executor produces "no kernel image is available" at the first CUDA call. `lium doctor` (`sdk/whoami-doctor`) warns about that combination when it can derive it.
- **FlashAttention-3 is Hopper-only** (H100/H200). On Blackwell use FlashAttention-4 or PyTorch's SDPA (`torch.nn.attention.sdpa_kernel`).
- **Background processes must be detached:** `nohup setsid <cmd> > log 2>&1 < /dev/null &`, or `lium exec -d`. A plain `nohup cmd &` through `lium exec` can die with the session.
- **Minimal images lack tools** scripts assume: `apt-get update && apt-get install -y rsync ffmpeg git`.
- **Set `--ttl` on every `up`.** Billing runs until the pod is removed, whether or not the job is alive.
- **Executor vs. template CUDA:** `lium ls --format json` reports `max_cuda_version` per node; choose a template whose CUDA build does not exceed it.

## 5. Discovering the shapes

`lium schema PodInfo ExecutorInfo Template` (`sdk/schema-command`) prints JSON Schema derived from the SDK dataclasses. On main, `lium ps --format json` / `lium ls --format json` return the slimmer table-equivalent objects shown above, and `lium describe <pod> --json` returns the full manifest.

## 6. A complete run

```bash
#!/usr/bin/env bash
set -euo pipefail
export LIUM_NONINTERACTIVE=1 LIUM_OUTPUT=json

NODE=$(lium ls --gpu H100 --count 1 --format json | jq -r '.[0].huid')
NAME="job-$(date +%s)"
lium up "$NODE" --name "$NAME" --ttl 4h --yes --no-ssh
POD=$(lium ps --format json | jq -r --arg n "$NAME" '.[] | select(.name==$n) | .huid')
trap 'lium rm "$POD" --yes >/dev/null 2>&1 || true' EXIT

lium rsync "$POD" ./project /workspace/project
lium exec "$POD" --json "cd /workspace/project && python -m venv .venv && . .venv/bin/activate && pip install -q -r requirements.txt" >/dev/null
lium exec "$POD" --json "cd /workspace/project && . .venv/bin/activate && python train.py --epochs 1"
lium scp "$POD" /workspace/project/out ./out -d
```

## See also

- `docs/exit-codes.md` – error envelope and exit codes (`sdk/structured-errors`)
- "First hour on a Lium pod" in `docs/getting-started.rst` (`cli/docs-first-hour`)
- README – full command list and SDK quick start
