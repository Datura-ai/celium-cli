"""scripts/changelog.py folds changelog.d/*.md into CHANGELOG.md the way changelog.d/README.md says."""
import importlib.util
import pathlib
import subprocess
import sys

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "changelog.py"
spec = importlib.util.spec_from_file_location("changelog_script", SCRIPT)
changelog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changelog)

RELEASED = "# Changelog\n\nintro\n\n## [0.0.37] - 2026-09-08\n\n### Added\n- old\n"


def _repo(tmp_path, changelog_text=RELEASED, fragments=None):
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "README.md").write_text("# not a fragment\n", encoding="utf-8")
    for name, body in (fragments or {}).items():
        (tmp_path / "changelog.d" / name).write_text(body, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(changelog_text, encoding="utf-8")
    return tmp_path


def test_collect_merges_same_named_sections_and_skips_the_readme(tmp_path):
    root = _repo(tmp_path, fragments={
        "DAH-1.md": "### Fixed\n- one\n",
        "DAH-2.md": "### Added\n- two\n\n### fixed\n- three\n",
        "DAH-3.md": "- loose line goes under Changed\n",
    })
    files, sections = changelog.collect(root / "changelog.d")
    assert [f.name for f in files] == ["DAH-1.md", "DAH-2.md", "DAH-3.md"]
    assert sections == {"Added": ["- two"], "Changed": ["- loose line goes under Changed"], "Fixed": ["- one", "- three"]}


def test_render_uses_the_canonical_section_order():
    text = changelog.render("0.0.38", "2026-09-09", {"Fixed": ["- f"], "Added": ["- a"]})
    assert text.startswith("## [0.0.38] - 2026-09-09\n\n### Added\n- a\n\n### Fixed\n- f\n")


def test_release_goes_above_the_newest_released_version():
    out = changelog.insert_release(RELEASED, "## [0.0.38] - 2026-09-09\n\n### Fixed\n- f\n")
    assert out.index("## [0.0.38]") < out.index("## [0.0.37]")
    assert out.startswith("# Changelog\n\nintro\n\n")


def test_release_goes_below_an_unreleased_block_not_above_it():
    # arhangel66's review: six open PRs still add `## [Unreleased]`; the fold must not land above it.
    text = "# Changelog\n\n## [Unreleased]\n\n### Added\n- pending\n\n## [0.0.37] - 2026-09-08\n\n- old\n"
    out = changelog.insert_release(text, "## [0.0.38] - 2026-09-09\n\n### Fixed\n- f\n")
    assert out.index("## [Unreleased]") < out.index("## [0.0.38]") < out.index("## [0.0.37]")


def test_release_is_appended_when_there_is_no_released_version_yet():
    out = changelog.insert_release("# Changelog\n\nintro\n", "## [0.0.1] - 2026-09-09\n\n### Added\n- a\n")
    assert out.endswith("## [0.0.1] - 2026-09-09\n\n### Added\n- a\n\n")


def test_dry_run_prints_the_section_and_writes_nothing(tmp_path):
    root = _repo(tmp_path, fragments={"DAH-1.md": "### Fixed\n- one\n"})
    r = subprocess.run([sys.executable, str(SCRIPT), "--version", "0.0.38", "--date", "2026-09-09", "--dry-run", "--root", str(root)],
                       capture_output=True, text=True, check=True)
    assert r.stdout.startswith("## [0.0.38] - 2026-09-09\n\n### Fixed\n- one\n")
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == RELEASED
    assert (root / "changelog.d" / "DAH-1.md").exists()


def test_fold_rewrites_changelog_and_deletes_only_the_fragments(tmp_path):
    root = _repo(tmp_path, fragments={"DAH-1.md": "### Fixed\n- one\n", "DAH-2.md": "### Added\n- two\n"})
    r = subprocess.run([sys.executable, str(SCRIPT), "--version", "0.0.38", "--date", "2026-09-09", "--root", str(root)],
                       capture_output=True, text=True, check=True)
    assert r.stdout.strip() == "CHANGELOG.md: added [0.0.38] from 2 fragment(s)"
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.0.38] - 2026-09-09\n\n### Added\n- two\n\n### Fixed\n- one\n" in text
    assert text.index("## [0.0.38]") < text.index("## [0.0.37]")
    assert sorted(p.name for p in (root / "changelog.d").iterdir()) == ["README.md"]


def test_no_fragments_is_an_error_not_an_empty_release(tmp_path):
    root = _repo(tmp_path)
    r = subprocess.run([sys.executable, str(SCRIPT), "--version", "0.0.38", "--root", str(root)], capture_output=True, text=True)
    assert r.returncode != 0
    assert "no fragments" in r.stderr
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == RELEASED
