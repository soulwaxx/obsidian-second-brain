#!/usr/bin/env python3
"""Boundary and content regressions for the wiki Python middleware."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

middleware = Path(sys.argv[1]).resolve()
migration = Path(sys.argv[2]).resolve()


def run(script: Path, *args: str, ok: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, str(script), *map(str, args)],
        text=True, capture_output=True, check=False,
    )
    if ok and result.returncode:
        raise AssertionError(f"{script.name} unexpectedly failed: {result.stderr}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"{script.name} unexpectedly succeeded: {args}")
    return result


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp) / "vault"
    wiki = root / "wiki"
    topic = wiki / "topic"
    outside = root / "outside"
    topic.mkdir(parents=True)
    outside.mkdir()
    (wiki / ".raw").mkdir()
    page = topic / "page.md"
    page.write_text('---\ntype: note\ntitle: "A [link]"\n'
                    'description: >\n  first line\n  second line\n---\n[[dest]]\n')
    (wiki / "dest.md").write_text("---\ntype: note\ntitle: Dest\n---\n")
    foreign = outside / "foreign.md"
    foreign.write_text("---\ntype: note\n---\n[[dest]]\n")
    (wiki / "alias").symlink_to(outside, target_is_directory=True)
    (wiki / "foreign.md").symlink_to(foreign)

    guard = middleware / "guard.py"
    run(guard, root, "wiki/topic/page.md")
    run(guard, root, "wiki/_plan.md")
    run(guard, "--if-wiki", root, "outside/other.md")
    for blocked in ("wiki/index.md", "wiki/topic/index.md", "wiki/log.md",
                    "wiki/topic/log.md", "wiki/.raw/secret.env",
                    "wiki/alias/new.md", "wiki/foreign.md", "wiki/../outside/foreign.md"):
        run(guard, root, blocked, ok=False)
    # An absolute path that uses macOS's /var alias must still be recognized.
    run(guard, "--if-wiki", root, page)

    validator = middleware / "validate.py"
    run(validator, page)
    invalid = wiki / "invalid.md"
    invalid.write_text("---\ntype: note\n---not-a-delimiter\n")
    run(validator, invalid, ok=False)
    invalid.unlink()

    sync = middleware / "sync.py"
    for directory in ("../outside", "alias"):
        run(sync, root, "--dir", directory, ok=False)
    assert not (outside / "index.md").exists()
    run(sync, root)
    index = (wiki / "index.md").read_text()
    assert "[alias/]" not in index and "[foreign]" not in index
    assert "[A \\[link\\]](page.md) - first line second line" in (topic / "index.md").read_text()
    assert "[topic/](topic/)" in index
    lint = json.loads(run(middleware / "lint.py", root).stdout)
    assert not lint["stale_index_entries"] and not lint["dead_links"]

    run(migration, root, "--apply")
    assert foreign.read_text().endswith("[[dest]]\n")
    assert "[Dest](/dest.md)" in page.read_text()

print("wiki middleware boundary regressions PASS")
