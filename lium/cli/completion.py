"""Shell completion setup for Lium CLI."""

import os
from functools import cache
from pathlib import Path
from typing import Dict, Tuple, List

from lium.sdk import Lium

# Shell configurations: (config_file, completion_script)
SHELLS: Dict[str, Tuple[str, str]] = {
    "bash": ("~/.bashrc", 'command -v lium >/dev/null 2>&1 && eval "$(_LIUM_COMPLETE=bash_source lium)"'),
    "zsh": ("~/.zshrc", 'command -v lium >/dev/null 2>&1 && eval "$(_LIUM_COMPLETE=zsh_source lium)"'),
    "fish": ("~/.config/fish/config.fish", "command -v lium >/dev/null 2>&1 && _LIUM_COMPLETE=fish_source lium | source")
}


def ensure_completion() -> None:
    """Silently ensure shell completion is installed."""
    # Check if already processed this installation
    marker_file = Path.home() / ".lium_completion_installed"
    if marker_file.exists():
        return

    shell = os.path.basename(os.environ.get("SHELL", "bash"))
    if shell not in SHELLS:
        return

    config_file, script = SHELLS[shell]
    config_path = Path(config_file).expanduser()

    # Check if already installed in shell config
    try:
        if config_path.exists() and "_LIUM_COMPLETE" in config_path.read_text():
            # Mark as installed to avoid future checks
            marker_file.touch()
            return
    except IOError:
        return

    # Install completion and notify user
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with config_path.open("a") as f:
            f.write(f"\n# Lium CLI completion\n{script}\n")
        # Mark as installed
        marker_file.touch()

        # stderr, so a command invoked with --json still emits clean stdout
        from .utils import notice_console as console
        console.success("✓ Shell completions have been configured for tab support")
        console.info("✓ Please restart your terminal or run:")
        console.info(f"  source {config_file}")
        console.print()

    except IOError:
        pass  # Silent fail


@cache
def _get_full_gpu_types() -> List[str]:
    return sorted(list(Lium().gpu_types()))


def get_gpu_completions(ctx, param, incomplete):
    try:
        return [f for f in _get_full_gpu_types() if f.startswith(incomplete.upper())]
    except Exception:
        # silent fail
        return []
