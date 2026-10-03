"""Publishing guard: no private addresses, home paths or site identifiers in tracked files.
Extra site-specific words can be listed (one per line) in a local, untracked file named by $GQ_DENYLIST."""
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = [
    (re.compile(r"\b(10\.\d{1,3}|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"), "private IP"),
    (re.compile(r"/home/(?!alice\b|user\b)[a-z][a-z0-9_-]+"), "home path"),
    (re.compile(r"[\w.+-]+@(?!users\.noreply\.github\.com|example\.(com|org))[\w-]+\.(edu|ac\.\w+|in)\b"), "institution email"),
]


def tracked():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    skip = {".git", ".venv", "__pycache__", ".pytest_cache", "build", "dist"}
    return [ROOT / f for f in out if (ROOT / f).is_file()] or \
        [p for p in ROOT.rglob("*") if p.is_file() and not skip & set(p.parts) and not p.name.endswith(".egg-info")
         and ".egg-info" not in str(p)]


def test_no_secrets():
    words = []
    if os.environ.get("GQ_DENYLIST") and Path(os.environ["GQ_DENYLIST"]).exists():
        words = [w.strip() for w in Path(os.environ["GQ_DENYLIST"]).read_text().splitlines() if w.strip()]
    hits = []
    for f in tracked():
        if f.suffix in (".png", ".gif", ".svg") or f.name == "test_no_secrets.py":
            continue
        text = f.read_text(errors="ignore")
        for rx, what in PATTERNS:
            for m in rx.finditer(text):
                if what == "private IP" and m.group(0).startswith("10.0.0."):
                    continue  # documentation example range used in tests
                hits.append(f"{f.relative_to(ROOT)}: {what}: {m.group(0)}")
        for w in words:
            if re.search(r"\b%s\b" % re.escape(w), text, re.I):
                hits.append(f"{f.relative_to(ROOT)}: denylisted word")
    assert not hits, "\n".join(hits)
