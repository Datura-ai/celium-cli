from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MINE_INSTALLER = REPOSITORY_ROOT / "mine.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def test_mine_installer_upgrades_uv_managed_cli(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "calls.log"

    _write_executable(
        bin_dir / "uv",
        """#!/bin/sh
printf 'uv %s\\n' "$*" >> "$CALL_LOG"
if [ "$1" = "tool" ] && [ "$2" = "list" ]; then
    printf 'lium.io v0.0.32\\n- lium\\n'
fi
""",
    )
    _write_executable(
        bin_dir / "lium",
        """#!/bin/sh
printf 'lium %s\\n' "$*" >> "$CALL_LOG"
if [ "$1" = "--version" ]; then
    printf 'lium, version 0.0.32\\n'
fi
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "CALL_LOG": str(call_log),
            "LIUM_INSTALLER_REEXEC": "1",
            "PATH": f"{bin_dir}:{env['PATH']}",
        }
    )

    result = subprocess.run(
        ["bash", str(MINE_INSTALLER), "--auto"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = call_log.read_text().splitlines()
    assert "uv tool list" in calls
    assert "uv tool install --upgrade lium.io" in calls
    assert "lium --version" in calls
    assert "lium mine --auto" in calls
    assert "Updating lium-cli" in result.stdout
