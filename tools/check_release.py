#!/usr/bin/env python3
"""Validate the package version and release tag."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
STABLE_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def release_version(root: Path = ROOT) -> str:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not STABLE_VERSION.fullmatch(version):
        raise ValueError("VERSION must contain one stable MAJOR.MINOR.PATCH version")
    return version


def package_version(root: Path = ROOT) -> str:
    with (root / "pyproject.toml").open("rb") as manifest:
        return tomllib.load(manifest)["project"]["version"]


def validate_manifest(root: Path = ROOT) -> str:
    version = release_version(root)
    packaged = package_version(root)
    if packaged != version:
        raise ValueError(
            f"pyproject.toml version {packaged} does not match VERSION {version}"
        )
    return version


def expected_tag(root: Path = ROOT) -> str:
    return f"v{validate_manifest(root)}"


def git(*arguments: str, root: Path = ROOT, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def validate_tag(tag: str, root: Path = ROOT) -> str:
    expected = expected_tag(root)
    if tag != expected:
        raise ValueError(f"expected tag {expected}, got {tag}")
    return expected


def validate_tag_available(root: Path = ROOT) -> str:
    tag = expected_tag(root)
    existing = git("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}", root=root, check=False)
    if existing:
        raise ValueError(f"release tag already exists: {tag}")
    return tag


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=("all", "tag", "available"))
    parser.add_argument("value", nargs="?")
    args = parser.parse_args()

    try:
        if args.target == "all":
            if args.value:
                parser.error("all does not accept a value")
            print(f"Validated version {validate_manifest()}")
        elif args.target == "tag":
            if not args.value:
                parser.error("tag requires a value")
            print(f"Validated release tag {validate_tag(args.value)}")
        else:
            if args.value:
                parser.error("available does not accept a value")
            print(f"Validated release tag availability for {validate_tag_available()}")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
