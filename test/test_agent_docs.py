"""docs/agents.md must describe flags and commands that exist.

Every ``lium …`` invocation on the page (fenced blocks and inline code) is resolved against the click command tree:
the subcommand chain must exist and every ``--flag`` / ``-x`` on the line must be a parameter of that command.
Environment variables the page names as ours (``LIUM_*``) must appear in the source tree. The page cannot drift
from the CLI it documents (review wave 7 Sep 2026: docs described ``exec -d``, ``up --verify-gpus`` and four
commands that did not exist on the branch).
"""

import re
from pathlib import Path

import click
import pytest

from lium.cli.cli import cli

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DOC = ROOT / "docs" / "agents.md"


def _strip_quoted(s: str) -> str:
    return re.sub(r"\"[^\"]*\"|'[^']*'", " ", s)


def _lium_invocations(text: str):
    """(line_no, tokens) for every `lium …` command on the page, cut at shell operators outside quotes."""
    out = []
    for no, line in enumerate(text.splitlines(), 1):
        line = re.sub(r"<[^<>\n]{1,40}>", "PLACEHOLDER", line)   # `<pod>` is an argument, not a redirect
        for m in re.finditer(r"(?:^|[\s`$>(])lium\s+(?=\S)", line):
            rest, cmd, quote = line[m.end():], "", None
            for ch in rest:
                if quote:
                    if ch == quote:
                        quote = None
                elif ch in "\"'":
                    quote = ch
                elif ch in "|&;>#`":
                    break
                cmd += ch
            toks = _strip_quoted(cmd).split()
            if "--" in toks:
                toks = toks[: toks.index("--")]
            if toks and re.fullmatch(r"[a-z][a-z0-9-]*", toks[0]) and toks[0] != "PLACEHOLDER":
                out.append((no, toks))   # `lium = Lium()` is Python, not an invocation
    return out


def _resolve(tokens):
    """→ (command, chain, flags) or None when the first token is prose (e.g. 'lium requires …')."""
    command, chain, i = cli, [], 0
    while i < len(tokens) and isinstance(command, click.Group) and tokens[i] in command.commands:
        command = command.commands[tokens[i]]
        chain.append(tokens[i])
        i += 1
    if not chain:
        return None
    flags = [re.sub(r"=.*$", "", t) for t in tokens if re.fullmatch(r"--?[A-Za-z][\w-]*(=\S*)?", t)]
    return command, chain, flags


def _code_blocks(text: str, fence: str) -> str:
    """The bodies of the page's shell code blocks only (prose lines such as "lium requires" are not commands)."""
    if fence == "md":
        return "\n".join(re.findall(r"```(?:bash|sh|shell|console)?\n(.*?)```", text, re.S))
    # rst: `.. code-block:: bash` bodies are the indented lines that follow
    blocks, out = re.split(r"^\.\. code-block:: (?:bash|sh|shell|console)\s*$", text, flags=re.M)[1:], []
    for block in blocks:
        body = []
        for line in block.splitlines()[1:]:
            if line.strip() == "" and not body:
                continue
            if line and not line.startswith((" ", "\t")):
                break
            body.append(line.strip())
        out.append("\n".join(body))
    return "\n".join(out)


# every `lium …` line the three guides show a reader: agents.md whole, README and getting-started's shell blocks
PAGES = {
    "docs/agents.md": AGENTS_DOC.read_text(),
    "README.md": _code_blocks((ROOT / "README.md").read_text(), "md"),
    "docs/getting-started.rst": _code_blocks((ROOT / "docs" / "getting-started.rst").read_text(), "rst"),
}
INVOCATIONS = [(page, no, toks) for page, text in PAGES.items() for no, toks in _lium_invocations(text)]


def test_the_pages_have_commands_to_check():
    per_page = {page: sum(1 for p, _, _ in INVOCATIONS if p == page) for page in PAGES}
    assert per_page["docs/agents.md"] >= 20 and per_page["README.md"] >= 20 and per_page["docs/getting-started.rst"] >= 1, per_page


@pytest.mark.parametrize("page, line_no, tokens", INVOCATIONS, ids=[f"{p.split('/')[-1]} L{n}: lium {' '.join(t[:3])}" for p, n, t in INVOCATIONS])
def test_every_documented_lium_line_resolves(page, line_no, tokens):
    resolved = _resolve(tokens)
    assert resolved is not None, f"{page}:{line_no}: `lium {tokens[0]}` is not a command"
    command, chain, flags = resolved
    known = {opt for param in command.params for opt in param.opts} | {"--help"}
    passthrough = command.context_settings.get("ignore_unknown_options") or command.context_settings.get("allow_extra_args")
    for flag in flags:
        if flag in ("-", "--") or (flag.startswith("-") and not flag.startswith("--") and len(flag) != 2):
            continue
        assert passthrough or flag in known, f"{page}:{line_no}: `lium {' '.join(chain)} {flag}` — no such option"


def test_env_vars_named_as_ours_exist():
    text = AGENTS_DOC.read_text()
    source = "\n".join(p.read_text() for p in (ROOT / "lium").rglob("*.py"))
    for var in sorted(set(re.findall(r"\bLIUM_[A-Z0-9_]+\b", text))):
        assert var in source, f"docs/agents.md names `{var}` but nothing in lium/ reads it"


def test_guide_exists_and_readme_links_it():
    readme = (ROOT / "README.md").read_text()

    assert AGENTS_DOC.exists()
    assert "docs/agents.md" in readme


def test_guide_covers_the_lifecycle_and_the_gotchas():
    text = AGENTS_DOC.read_text()

    for needle in (
        "LIUM_API_KEY", "--format json", "--ttl", "--yes", "--no-ssh", "lium rm",
        '"ok": false', "/workspace", "/root", "HF_HOME", "PEP 668", "cu128", "FlashAttention-3",
        "nohup setsid", "< /dev/null",
    ):
        assert needle in text, needle


def test_guide_does_not_promise_unmerged_features():
    """The page describes this CLI; branch names and features that live elsewhere do not belong on it."""
    text = AGENTS_DOC.read_text()
    for banned in ("origin/main", "sdk/", "cli/", "--verify-gpus", "lium top", "lium cp", "lium doctor", "lium schema", "LIUM_NONINTERACTIVE", "LIUM_OUTPUT"):
        assert banned not in text, f"docs/agents.md still mentions `{banned}`"
