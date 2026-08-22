from __future__ import annotations

from pathlib import Path

from tools import scan_tracked_secrets


def test_scan_flags_forbidden_tracked_runtime_or_secret_paths(monkeypatch) -> None:
    root = Path("C:/repository")
    monkeypatch.setattr(scan_tracked_secrets, "ROOT", root)
    monkeypatch.setattr(
        scan_tracked_secrets,
        "_tracked_files",
        lambda: (
            root / "src/module/__pycache__/item.pyc",
            root / "trading.db",
            root / ".env",
            root / "secrets/deploy.key",
        ),
    )

    findings = scan_tracked_secrets.scan()

    assert {(path, rule) for path, _, rule in findings} == {
        ("src/module/__pycache__/item.pyc", "forbidden_tracked_path"),
        ("trading.db", "forbidden_tracked_path"),
        (".env", "forbidden_tracked_path"),
        ("secrets/deploy.key", "forbidden_tracked_path"),
    }
