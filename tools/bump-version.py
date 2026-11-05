#!/usr/bin/env python3
"""
Bumps modVersion in gradle.properties and prints the new version.

Usage:
  python tools/bump-version.py patch|minor|major   # 0.1.0 -> 0.1.1 / 0.2.0 / 1.0.0
  python tools/bump-version.py set 0.4.2           # an explicit version
  python tools/bump-version.py current             # just print it

Releases are tagged with exactly this version (no "v" prefix): the vanilla launcher profile and
the Dove launcher download the release files from that tag.
"""

import os
import re
import sys

PROPERTIES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gradle.properties")
LINE = re.compile(r"^(modVersion\s*=\s*)(\S+)\s*$", re.MULTILINE)
VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("patch", "minor", "major", "set", "current"):
        sys.exit(__doc__)
    with open(PROPERTIES, encoding="utf-8") as f:
        text = f.read()
    match = LINE.search(text)
    if not match:
        sys.exit("modVersion not found in gradle.properties")
    current = match.group(2)
    action = sys.argv[1]
    if action == "current":
        print(current)
        return
    if action == "set":
        if len(sys.argv) != 3 or not VERSION.match(sys.argv[2]):
            sys.exit("set needs a MAJOR.MINOR.PATCH version")
        new = sys.argv[2]
    else:
        parts = VERSION.match(current)
        if not parts:
            sys.exit(f"modVersion {current!r} is not MAJOR.MINOR.PATCH; use 'set' first")
        major, minor, patch = (int(p) for p in parts.groups())
        if action == "major":
            major, minor, patch = major + 1, 0, 0
        elif action == "minor":
            minor, patch = minor + 1, 0
        else:
            patch += 1
        new = f"{major}.{minor}.{patch}"
    with open(PROPERTIES, "w", encoding="utf-8", newline="\n") as f:
        f.write(LINE.sub(lambda m: m.group(1) + new, text, count=1))
    print(new)


if __name__ == "__main__":
    main()
