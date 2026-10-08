"""Snapshot the installed dependency closure for this exact Python/platform.

This is an offline environment lock generator, not a cross-platform resolver.
Run it on the deployment platform after a clean install and successful checks.
"""

import argparse
from importlib.metadata import distribution
from pathlib import Path
import platform

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def roots(path):
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            yield from roots(path.parent / line[3:].strip())
        else:
            yield Requirement(line)


def snapshot(requirements):
    queue = [(item.name, item.extras) for item in roots(requirements)]
    visited = set()
    locked = {}
    environment = default_environment()
    while queue:
        name, extras = queue.pop()
        key = (canonicalize_name(name), tuple(sorted(extras)))
        if key in visited:
            continue
        visited.add(key)
        installed = distribution(name)
        locked[canonicalize_name(name)] = installed.version
        for raw in installed.requires or []:
            dependency = Requirement(raw)
            if dependency.marker and not any(
                dependency.marker.evaluate({**environment, "extra": extra}) for extra in {"", *extras}
            ):
                continue
            queue.append((dependency.name, dependency.extras))
    return locked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, default=Path("requirements.txt"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packages = snapshot(args.requirements)
    header = f"# Installed closure: {platform.system()} {platform.machine()}, Python {platform.python_version()}\n"
    args.output.write_text(header + "# Reuse only on the same target platform; no artifact hashes are included.\n" +
                           "".join(f"{name}=={version}\n" for name, version in sorted(packages.items())), encoding="utf-8")


if __name__ == "__main__":
    main()
