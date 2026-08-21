"""Fail-closed validation for deployment image references."""

from __future__ import annotations

import argparse
import re

_IMMUTABLE_IMAGE = re.compile(
    r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?:/[a-z0-9][a-z0-9._-]*)*"
    r"@sha256:[0-9a-f]{64}$"
)


def validate_immutable_image_reference(image: str) -> str:
    """Return an image reference only when it has a complete SHA-256 digest."""

    if not _IMMUTABLE_IMAGE.fullmatch(image):
        raise ValueError("image must be repository@sha256:<64 lowercase hex> form")
    return image


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    arguments = parser.parse_args()
    print(validate_immutable_image_reference(arguments.image))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
