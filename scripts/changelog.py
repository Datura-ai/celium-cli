#!/usr/bin/env python3
"""Fold changelog.d/*.md fragments into CHANGELOG.md under a new version heading (Keep a Changelog layout).

    python scripts/changelog.py --version 0.5.0            # writes CHANGELOG.md, deletes the fragments
    python scripts/changelog.py --version 0.5.0 --dry-run  # prints the new section only

Each fragment holds `### Added|Changed|Deprecated|Removed|Fixed|Security` headings followed by bullets (see
changelog.d/README.md). Same-named sections are merged in the canonical order; bullets keep the order of the fragment
file names. Lines outside a section go under `### Changed`.
"""
import argparse, datetime, pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SECTIONS = ["Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"]


def collect(frag_dir: pathlib.Path):
    sections = {s: [] for s in SECTIONS}
    files = sorted(p for p in frag_dir.glob("*.md") if p.name != "README.md")
    for f in files:
        current = "Changed"
        for line in f.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^###\s+(\w+)", line)
            if m:
                name = m.group(1).capitalize()
                current = name if name in SECTIONS else "Changed"
                continue
            if re.match(r"^##\s", line) or not line.strip():
                continue
            sections[current].append(line.rstrip())
    return files, {k: v for k, v in sections.items() if v}


def render(version: str, date: str, sections: dict) -> str:
    out = [f"## [{version}] - {date}", ""]
    for name in SECTIONS:
        if name in sections:
            out.append(f"### {name}")
            out.extend(sections[name])
            out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--version", required=True, help="version heading to write, e.g. 0.5.0")
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    frag_dir = ROOT / "changelog.d"
    files, sections = collect(frag_dir)
    if not sections:
        sys.exit("changelog.d/ has no fragments")
    section = render(a.version, a.date, sections)
    if a.dry_run:
        print(section)
        return
    path = ROOT / "CHANGELOG.md"
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^## \[", text, re.M)  # insert before the first existing release heading
    idx = m.start() if m else len(text)
    path.write_text(text[:idx] + section + "\n" + text[idx:], encoding="utf-8")
    for f in files:
        f.unlink()
    print(f"CHANGELOG.md: added [{a.version}] from {len(files)} fragment(s)")


if __name__ == "__main__":
    main()
