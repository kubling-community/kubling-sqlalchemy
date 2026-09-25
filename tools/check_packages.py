#!/usr/bin/env python3
"""Inspect the wheel and source distribution built for release."""

from __future__ import annotations

from email.parser import Parser
from pathlib import Path
import tarfile
import zipfile

from check_release import release_version


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = (
    "PLAN_GRPC.md",
    "Jenkinsfile",
    "Dockerfile",
    "conn_test.py",
    "superset_build/",
    "tests/",
    "scripts/",
    "__pycache__/",
)
PACKAGE_FILES = (
    "kubling_sqlalchemy/__init__.py",
    "kubling_sqlalchemy/dialect.py",
    "kubling_sqlalchemy/reflection.py",
    "kubling_sqlalchemy/dbapi/connection.py",
    "kubling_sqlalchemy/dbapi/cursor.py",
    "kubling_sqlalchemy/transport/client.py",
    "kubling_sqlalchemy/transport/codec.py",
)


def require_members(names: list[str], required: tuple[str, ...]) -> None:
    missing = [name for name in required if name not in names]
    if missing:
        raise ValueError(f"missing package members: {missing}")
    leaked = [name for name in names if any(item in name for item in FORBIDDEN)]
    if leaked:
        raise ValueError(f"non-distribution files leaked into package: {leaked}")


def inspect_wheel(wheel: Path, version: str) -> None:
    expected_prefix = f"kubling_sqlalchemy-{version}-"
    if not wheel.name.startswith(expected_prefix):
        raise ValueError(f"wheel does not match version {version}: {wheel.name}")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        require_members(names, PACKAGE_FILES)
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        entry_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        if len(metadata_names) != 1 or len(entry_names) != 1:
            raise ValueError("wheel must contain one metadata and one entry-point file")
        if not any(name.endswith(".dist-info/licenses/LICENSE") for name in names):
            raise ValueError("wheel license is missing")
        metadata = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
        if metadata["Name"] != "kubling-sqlalchemy" or metadata["Version"] != version:
            raise ValueError("wheel identity does not match the release")
        requirements = metadata.get_all("Requires-Dist", [])
        if not any(item.startswith("SQLAlchemy<2.1,>=2.0.36") for item in requirements):
            raise ValueError("wheel SQLAlchemy requirement is incorrect")
        if "kubling-grpc[status]==1.1.1" not in requirements:
            raise ValueError("wheel must pin kubling-grpc[status] 1.1.1")
        entry_points = archive.read(entry_names[0]).decode("utf-8")
        for name in ("kubling =", "kubling.grpc ="):
            if name not in entry_points:
                raise ValueError(f"missing SQLAlchemy entry point: {name}")


def inspect_sdist(source: Path, version: str) -> None:
    prefix = f"kubling_sqlalchemy-{version}"
    if source.name != prefix + ".tar.gz":
        raise ValueError(f"sdist does not match version {version}: {source.name}")
    with tarfile.open(source) as archive:
        members = archive.getnames()
    if any(name != prefix and not name.startswith(prefix + "/") for name in members):
        raise ValueError("sdist has an unexpected root directory")
    names = [name.partition("/")[2] for name in members if "/" in name]
    require_members(
        names,
        PACKAGE_FILES
        + ("LICENSE", "README.md", "RELEASING.md", "VERSION", "pyproject.toml"),
    )


def main() -> None:
    version = release_version()
    wheels = list((ROOT / "dist").glob("*.whl"))
    sources = list((ROOT / "dist").glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("expected exactly one wheel and one source distribution")
    inspect_wheel(wheels[0], version)
    inspect_sdist(sources[0], version)
    print(f"Validated kubling-sqlalchemy {version} distributions")


if __name__ == "__main__":
    main()
