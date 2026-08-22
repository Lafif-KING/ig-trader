from pathlib import Path


def test_shadow_image_is_digest_pinned_non_root_and_excludes_execution_files() -> None:
    source = Path("Dockerfile.shadow").read_text(encoding="utf-8")
    assert "@sha256:" in source
    assert "USER 10001:10001" in source
    assert "EXECUTION_MODE=SHADOW_DEMO" in source
    for path in ("execution.py", "main.py", "db_bootstrap.py"):
        assert f"rm -f ./src/ig_trader/{path}" in source


def test_shadow_source_has_no_broker_order_http_path() -> None:
    for path in Path("src/ig_trader").glob("shadow_*.py"):
        source = path.read_text(encoding="utf-8")
        for prohibited in ("/positions", "/workingorders", '"POST"', '"PUT"', '"DELETE"'):
            assert prohibited not in source
