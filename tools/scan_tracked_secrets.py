"""Dependency-free, redacting secret-pattern gate for tracked repository files."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_HIGH_CONFIDENCE = {
    "aws_access_key": re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    "github_pat": re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA|EC|OPENSSH|DSA)? ?PRIVATE KEY-----"),
    "slack_token": re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
}
_LITERAL_SECRET = re.compile(
    r"(?i)(?:api[_-]?key|client[_-]?secret|password|private[_-]?key|token)"
    r"\s*[:=]\s*['\"]([^'\"\r\n]{12,})['\"]"
)
_PLACEHOLDER = re.compile(r"(?i)(?:dummy|example|placeholder|redacted|test|never[-_ ]?load)")
_TEXT_SUFFIXES = {
    "",
    ".bicep",
    ".cfg",
    ".env",
    ".example",
    ".ini",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".sql",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


def _tracked_files() -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return tuple(ROOT / value.decode("utf-8") for value in result.stdout.split(b"\0") if value)


def scan() -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for path in _tracked_files():
        if not path.is_file() or path.suffix.casefold() not in _TEXT_SUFFIXES:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(ROOT).as_posix()
        for line_number, line in enumerate(content.splitlines(), start=1):
            for rule, pattern in _HIGH_CONFIDENCE.items():
                if pattern.search(line):
                    findings.append((relative, line_number, rule))
            literal = _LITERAL_SECRET.search(line)
            if literal and not _PLACEHOLDER.search(literal.group(1)):
                findings.append((relative, line_number, "literal_secret_assignment"))
    return findings


def main() -> int:
    findings = scan()
    if findings:
        for path, line, rule in findings:
            print(f"secret-pattern finding: {path}:{line} rule={rule}")
        return 1
    print("secret-pattern scan passed; findings=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
