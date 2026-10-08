"""Keep the active release and two rollback environments after a healthy deploy."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import time


def cleanup_releases(root: Path, previous: Path | None = None) -> dict:
    root = root.resolve(strict=True)
    releases = root / "releases"
    if releases.is_symlink() or not releases.is_dir() or releases.resolve().parent != root:
        raise ValueError("Releases must be a real directory under the application root")
    current = root / "current"
    if not current.is_symlink():
        raise ValueError("Current must be a release symlink")
    active = current.resolve(strict=True)
    if active.parent != releases:
        raise ValueError("Active release must be directly under releases")

    # A still-running worker may be using an older release after activation.
    busy = set()
    proc = Path("/proc")
    if proc.is_dir():
        for process in proc.iterdir():
            if not process.name.isdigit():
                continue
            for resource in ("cwd", "exe"):
                try:
                    path = (process / resource).resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                for parent in (path, *path.parents):
                    if parent.parent == releases:
                        busy.add(parent)

    entries = [path for path in releases.iterdir() if path.is_dir() and not path.is_symlink()]
    candidates = [path for path in entries if path != active and path.name != "legacy"
                  and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", path.name)
                  and (path / ".venv" / "bin" / "python").is_file()]

    def deployed_at(path):
        marker = path / ".deployment-success"
        return marker.stat().st_mtime if marker.is_file() else path.stat().st_mtime

    candidates.sort(key=deployed_at, reverse=True)
    rollback = []
    if previous is not None:
        previous = previous.resolve(strict=True)
        if previous.parent != releases:
            raise ValueError("Previous release must be directly under releases")
        if previous in candidates:
            rollback.append(previous)
    rollback.extend(path for path in candidates if path not in rollback)
    keep = {active, releases / "legacy", *rollback[:2], *busy}
    stale_staging = [path for path in entries
                     if re.fullmatch(r"\.staging-[A-Za-z0-9][A-Za-z0-9._-]{0,79}-[0-9]+", path.name)
                     and time.time() - path.stat().st_mtime > 86400]
    targets = [path for path in candidates + stale_staging if path not in keep]

    # Validate the complete plan before deleting anything. Never follow runtime links.
    for path in targets:
        if path.resolve().parent != releases or path.is_symlink() or os.path.ismount(path):
            raise ValueError(f"Unsafe cleanup target: {path.name}")
        for name in ("data", "vault", "output", ".env"):
            item = path / name
            if item.exists() and not item.is_symlink():
                raise ValueError(f"Release contains real runtime data: {path.name}/{name}")
        if any(os.path.ismount(Path(folder) / name)
               for folder, dirs, _files in os.walk(path, followlinks=False)
               for name in dirs if not (Path(folder) / name).is_symlink()):
            raise ValueError(f"Release contains a mounted directory: {path.name}")

    removed = []
    for path in targets:
        if current.resolve(strict=True) != active:
            raise RuntimeError("Active release changed during cleanup")
        shutil.rmtree(path)
        removed.append(path.name)
    return {"active": active.name, "kept": sorted(path.name for path in keep if path.exists()),
            "removed": removed}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args()
    print(json.dumps(cleanup_releases(args.root, args.previous), indent=2))
