#!/usr/bin/env python
"""Bump the version number of the project.
"""

from __future__ import annotations

import re
import sys
import logging
import subprocess as sp
from enum import Enum
from pathlib import Path
from argparse import ArgumentParser, Namespace
from functools import cached_property
from dataclasses import dataclass

from packaging.version import parse as parse_version, Version

PROJECT_DIR = Path(__file__).parent.parent

logger = logging.getLogger()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@dataclass
class Package:
    name: str
    version_files: list[Path]
    history_file: Path
    tag_format: str

    def __post_init__(self) -> None:
        packages[self.name] = self


packages: dict[str, Package] = {}

Package(
    name="psycopg",
    version_files=[
        PROJECT_DIR / "psycopg/psycopg/version.py",
        PROJECT_DIR / "psycopg_c/psycopg_c/version.py",
    ],
    history_file=PROJECT_DIR / "docs/news.rst",
    tag_format="{version}",
)

Package(
    name="psycopg_pool",
    version_files=[PROJECT_DIR / "psycopg_pool/psycopg_pool/version.py"],
    history_file=PROJECT_DIR / "docs/news_pool.rst",
    tag_format="pool-{version}",
)


class Bumper:
    def __init__(self, package: Package, *, bump_level: str | BumpLevel):
        self.package = package
        self.bump_level = BumpLevel(bump_level)

        self._version_regex = re.compile(
            r"""(?ix)
            ^
            (?P<pre>__version__\s*=\s*(?P<quote>["']))
            (?P<ver>[^'"]+)
            (?P<post>(?P=quote)\s*(?:\#.*)?)
            $
            """
        )

    @cached_property
    def current_version(self) -> Version:
        versions = set(
            self._parse_version_from_file(f) for f in self.package.version_files
        )
        if len(versions) > 1:
            raise ValueError(
                f"inconsistent versions ({', '.join(map(str, sorted(versions)))})"
                f" in {self.package.version_files}"
            )

        return versions.pop()

    @cached_property
    def want_version(self) -> Version:
        current = self.current_version
        parts = [current.major, current.minor, current.micro, current.dev or 0]

        match self.bump_level:
            case BumpLevel.MAJOR:
                # 1.2.3 -> 2.0.0
                parts[0] += 1
                parts[1] = parts[2] = parts[3] = 0
            case BumpLevel.MINOR:
                # 1.2.3 -> 1.3.0
                parts[1] += 1
                parts[2] = parts[3] = 0
            case BumpLevel.PATCH:
                # 1.2.3 -> 1.2.4
                # 1.2.3.dev4 -> 1.2.3
                if parts[3] == 0:
                    parts[2] += 1
                else:
                    parts[3] = 0
            case BumpLevel.DEV:
                # 1.2.3 -> 1.2.4.dev1
                # 1.2.3.dev1 -> 1.2.3.dev2
                if parts[3] == 0:
                    parts[2] += 1
                parts[3] += 1

        sparts = [str(part) for part in parts[:3]]
        if parts[3]:
            sparts.append(f"dev{parts[3]}")
        return Version(".".join(sparts))

    def update_files(self) -> None:
        for f in self.package.version_files:
            self._update_version_in_file(f, self.want_version)

        if self.bump_level != BumpLevel.DEV:
            self._update_history_file(self.package.history_file, self.want_version)

    def commit(self) -> None:
        logger.debug("committing version changes")
        msg = f"""\
chore: bump {self.package.name} package version to {self.want_version}
"""
        files = self.package.version_files + [self.package.history_file]
        cmdline = ["git", "commit", "-m", msg] + list(map(str, files))
        sp.check_call(cmdline)

    def create_tag(self) -> None:
        logger.debug("tagging version %s", self.want_version)
        tag_name = self.package.tag_format.format(version=self.want_version)
        changes = self._get_changes_lines(
            self.package.history_file,
            self.want_version,
        )
        msg = f"""\
{self.package.name} {self.want_version} released

{''.join(changes)}
"""
        cmdline = ["git", "tag", "-a", "-s", "-m", msg, tag_name]
        sp.check_call(cmdline)

    def _parse_version_from_file(self, fp: Path) -> Version:
        logger.debug("looking for version in %s", fp)
        matches = []
        with fp.open() as f:
            for line in f:
                m = self._version_regex.match(line)
                if m:
                    matches.append(m)

        if not matches:
            raise ValueError(f"no version found in {fp}")
        elif len(matches) > 1:
            raise ValueError(f"more than one version found in {fp}")

        vs = parse_version(matches[0].group("ver"))
        assert isinstance(vs, Version)
        return vs

    def _update_version_in_file(self, fp: Path, version: Version) -> None:
        logger.debug("upgrading version to %s in %s", version, fp)
        lines = []
        with fp.open() as f:
            for line in f:
                if self._version_regex.match(line):
                    line = self._version_regex.sub(f"\\g<pre>{version}\\g<post>", line)
                lines.append(line)

        with fp.open("w") as f:
            for line in lines:
                f.write(line)

    def _update_history_file(self, fp: Path, version: Version) -> None:
        logger.debug("upgrading history file %s", fp)
        with fp.open() as f:
            lines = f.readlines()

        vln: int = -1
        lns = self._find_lines(
            r"^[^\s]+ " + re.escape(str(version)) + r"\s*\(unreleased\)?$", lines
        )
        assert len(lns) <= 1
        if len(lns) == 1:
            vln = lns[0]
            lines[vln] = lines[vln].rsplit(None, 1)[0]
            lines[vln + 1] = lines[vln + 1][0] * len(lines[lns[0]])

        lns = self._find_lines("^Future", lines)
        assert len(lns) <= 1
        if len(lns) == 1:
            del lines[lns[0] : lns[0] + 3]
            if vln > lns[0]:
                vln -= 3

        lns = self._find_lines("^Current", lines)
        assert len(lns) <= 1
        if len(lns) == 1 and vln >= 0:
            clines = lines[lns[0] : lns[0] + 3]
            del lines[lns[0] : lns[0] + 3]
            if vln > lns[0]:
                vln -= 3
            lines[vln:vln] = clines

        with fp.open("w") as f:
            for line in lines:
                f.write(line)
                if not line.endswith("\n"):
                    f.write("\n")

    def _get_changes_lines(self, fp: Path, version: Version) -> list[str]:
        with fp.open() as f:
            lines = f.readlines()

        lns = self._find_lines(r"^[^\s]+ " + re.escape(str(version)), lines)
        assert len(lns) == 1
        start = end = lns[0] + 3
        while lines[end].rstrip():
            end += 1

        return lines[start:end]

    def _find_lines(self, pattern: str, lines: list[str]) -> list[int]:
        rv = []
        rex = re.compile(pattern)
        for i, line in enumerate(lines):
            if rex.match(line):
                rv.append(i)

        return rv


def main() -> int | None:
    opt = parse_cmdline()
    logger.setLevel(opt.loglevel)
    bumper = Bumper(packages[opt.package], bump_level=opt.level)
    logger.info("current version: %s", bumper.current_version)
    logger.info("bumping to version: %s", bumper.want_version)
    if not opt.dry_run:
        bumper.update_files()
        bumper.commit()
        if opt.level != BumpLevel.DEV:
            bumper.create_tag()

    return 0


class BumpLevel(str, Enum):
    MAJOR = "major"
    MINOR = "minor"
    PATCH = "patch"
    DEV = "dev"


def parse_cmdline() -> Namespace:
    parser = ArgumentParser(description=__doc__)

    parser.add_argument(
        "--level",
        choices=[level.value for level in BumpLevel],
        default=BumpLevel.PATCH.value,
        type=BumpLevel,
        help="the level to bump [default: %(default)s]",
    )

    parser.add_argument(
        "--package",
        choices=list(packages.keys()),
        default="psycopg",
        help="the package to bump version [default: %(default)s]",
    )

    parser.add_argument(
        "-n",
        "--dry-run",
        help="Just pretend",
        action="store_true",
    )

    g = parser.add_mutually_exclusive_group()
    g.add_argument(
        "-q",
        "--quiet",
        help="Talk less",
        dest="loglevel",
        action="store_const",
        const=logging.WARN,
        default=logging.INFO,
    )
    g.add_argument(
        "-v",
        "--verbose",
        help="Talk more",
        dest="loglevel",
        action="store_const",
        const=logging.DEBUG,
        default=logging.INFO,
    )
    opt = parser.parse_args()

    return opt


if __name__ == "__main__":
    sys.exit(main())