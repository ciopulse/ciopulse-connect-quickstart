"""The repo is public. No client names, no real keys, no transcript text in logs."""
from __future__ import annotations

import base64
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Client and partner names that must never appear in this public repo. Base64-encoded so the
# list itself is not a hit; decode with base64 -d if you need to read it.
FORBIDDEN = [base64.b64decode(w).decode() for w in ['Zm9ydGVzY3Vl', 'Zm1n', 'aG9yaXpvbg==', 'aXNodXBhbA==', 'amVzc2U=', 'dmFsaWFudHlz']]
KEY_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),                      # AWS access key id
    re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*\S+"),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)x-api-key\s*[:=]\s*['\"]?[A-Za-z0-9_-]{24,}"),
]
SELF = Path(__file__).resolve()
SKIP_DIRS = {".git", ".aws-sam", "__pycache__", ".pytest_cache", ".venv", "venv"}


def _files():
    for p in ROOT.rglob("*"):
        if p.is_file() and p != SELF and not (SKIP_DIRS & set(p.relative_to(ROOT).parts)):
            yield p


def test_no_client_names_anywhere():
    hits = []
    for p in _files():
        try:
            text = p.read_text(errors="ignore").lower()
        except OSError:
            continue
        for word in FORBIDDEN:
            if word in text:
                hits.append(f"{p.relative_to(ROOT)}: {word}")
    assert not hits, hits


def test_no_credentials_anywhere():
    hits = []
    for p in _files():
        text = p.read_text(errors="ignore")
        for pat in KEY_PATTERNS:
            if pat.search(text):
                hits.append(f"{p.relative_to(ROOT)}: {pat.pattern}")
    assert not hits, hits


def test_handler_never_logs_transcript_fields():
    src = (ROOT / "src" / "handler.py").read_text()
    # every log/print call site must be an EMF record or a JSON dict without turns/text/summary keys
    for line in src.splitlines():
        if "log." in line or "print(" in line:
            assert not re.search(r"['\"](turns|text|summary|Content)['\"]", line), line
