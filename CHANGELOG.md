# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- SDK: `Lium.up(..., wait=True, timeout=600)` returns the ready `PodInfo` (a timeout raises a `LiumError` that names the still-billing pod).
- SDK: `Lium.pod_by_name(name)` (name, huid or id); `with lium.rent(executor_id=..., ...) as pod:` rents a pod for the block and always removes it — on return, on exception, and when it never became ready.
- SDK: `Lium.exec(..., timeout=)` raises `TimeoutError` and closes the channel; `Lium.exec(..., detach=True, log_path=)` starts the command under `nohup setsid … < /dev/null &` and returns `{"pid", "log_path", "command"}` (`Lium.build_detached_command` is the reusable command line).
- SDK: `Lium.gpu_stats(pod)` returns `GpuStats` per GPU from `nvidia-smi --query-gpu`; `Lium.parse_gpu_stats(text)` is the parser.
- SDK: every model has `to_dict()` (nested models included, plus derived fields such as `PodInfo.host`/`ssh_port` and `ExecutorInfo.gpu_model`).
- `lium top <pod> [--watch N] [--all] [--format json]` shows per-GPU utilisation, memory, temperature and power over SSH (one row per GPU, a summary line, JSON per sample). Pods are sampled in parallel; a pod whose `nvidia-smi` fails is a row with the error and makes the command exit 1, without hiding the others.

### Fixed
- `Lium.up()` no longer retries the rent POST blindly. On a timeout, connection error, 429 or 5xx it first looks in `ps` for a pod with the requested name on the chosen node and returns that; only when nothing appeared is the request sent once more, with the same client-generated `Idempotency-Key` header. One `lium up` could previously create two pods.
- `Lium._request()` accepts `retry=False` for calls that must not be repeated automatically.

## [0.4.3] - 2025-10-23

### Added
- `lium reboot` command to reboot pods individually or in batches.
- `lium scp` downloads via the new `--download / -d` flag with smarter defaults.

### Changed
- Updated dependency to `lium-sdk` 0.2.12 to access the reboot helper.

## [0.3.0] - 2025-01-23

### Added
- **GPU Filtering for `lium up`**:
  - `--gpu` option to filter executors by GPU type (e.g., H200, A6000)
  - `-c/--count` option to filter by exact GPU count per pod
  - `--country` option to filter by ISO country code (e.g., US, SG)
- **Auto-selection with filters** - Automatically selects best executor when filters are provided
- **Enhanced Pareto frontier** - Prioritizes US location and high bandwidth when prices are equal
- **Better error messages** - Helpful tips when no executors match filters
- **Loading indicators** - Shows progress when searching for executors

### Changed
- **Refactored `up` command** - Cleaner code structure with extracted helper functions
- **SSH always connects** - Pod creation now always waits and connects via SSH
- **Consistent behavior** - Both interactive and non-interactive modes auto-connect
- **Improved executor ranking** - Better selection when multiple executors have same price

### Removed
- `-w/--wait` option from `up` command - Now always waits and connects

## [0.3.0-beta.1] - 2025-01-17

### Added
- **New Commands**:
  - `fund` command - Fund accounts with TAO directly from Bittensor wallet
  - `config` command - Manage CLI configuration and settings
  - `rsync` command - Efficient file synchronization with pods
  - `scp` command - Secure file copying to/from pods
  - `theme` command - Customize CLI appearance with automatic OS theme detection
- **ThemedConsole system** - OS-aware color schemes with light/dark mode auto-detection
- **Enhanced plugin system** - Better integration for extending CLI functionality

### Changed
- Improved executor selection storage mechanism
- Cleaner console output with reduced verbosity
- Updated `lium-sdk` dependency to version 0.2.4

### Removed
- Success message from init command for cleaner output
- Dind column from `ls` output

### Note
- `image` and `compose` commands temporarily disabled for beta.1, will be included in beta.2

## [0.2.2] - 2025-01-10

### Added
- `--version` option to display CLI version (reads from pyproject.toml)

### Changed
- Version is now dynamically read from pyproject.toml for single source of truth

### Removed
- Deleted unused `lium_cli/` folder - project now uses `cli/` exclusively

## [0.2.1] - 2025-01-10

### Fixed
- Fixed package structure in pyproject.toml to properly include the `cli` module
- Resolved ModuleNotFoundError when installing from PyPI

## [0.2.0] - 2025-01-10

### Added
- **init command** - Interactive setup wizard for first-time users
- Pareto frontier optimization in `ls` command - shows optimal executors with ★ indicator
- Index-based executor selection in `up` command - use numbers from previous `ls` output
- Session storage for executor selection - enables quick selection by index
- Full-width tables across all commands for better terminal utilization
- Proper GPU configuration display in `ps` command showing actual GPU types
- Cost tracking in `ps` command showing spent amount and hourly rate
- Better error messages guiding users to run `lium init` when API key is missing

### Changed
- **BREAKING**: Now requires `lium-sdk>=0.2.0` from PyPI (no longer uses git dependency)
- Improved `ls` command with tighter table design and no units for cleaner display
- Enhanced `up` command with better template selection and confirmation flow
- Better `ps` command showing active pods with proper GPU configs and SSH commands
- Improved `templates` command removing unnecessary ID column and giving more space to tags
- Cleaner code throughout following PEP conventions
- All imports organized alphabetically and grouped properly
- Removed `from __future__ import annotations` (not needed in Python 3.9+)

### Fixed
- Rich markup issues with version numbers showing as colors
- SSH command generation missing spaces
- GPU configuration showing generic "GPU" instead of actual type
- Template selection colors and display issues
- Tables not taking full terminal width
- Various import and type hint improvements
- Bare except clauses replaced with specific exceptions

### Removed
- Unnecessary borders and decorations from tables
- Units (like "Gi") from numeric displays for cleaner look
- Confusing HUID column from templates display
- Debug code and trailing whitespace
- Unused helper functions

## [0.1.1] - Previous Release

- Initial release with basic pod management functionality
- Basic commands: ls, up, ps, exec, ssh, rm, templates
- Integration with lium-sdk from git
- Rich terminal UI with tables
- Basic error handling
