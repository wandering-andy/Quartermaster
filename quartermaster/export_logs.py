"""Copy finalized Quartermaster log artifacts to an external directory."""

import shutil
from pathlib import Path

SUBDIRECTORIES = ("logs", "recon")


def export(root, destination):
    root = Path(root).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if destination == root or root in destination.parents:
        raise ValueError("Export destination must be outside the Quartermaster data directory")

    copied = []
    for subdirectory in SUBDIRECTORIES:
        source = root / subdirectory
        if not source.is_dir():
            continue
        for path in sorted(source.iterdir()):
            if path.name.startswith(".") or path.suffix == ".part" or path.name.endswith(".tmp"):
                continue
            if not path.is_file():
                continue
            target_dir = destination / subdirectory
            target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = target_dir / path.name
            shutil.copy2(path, target)
            copied.append(target)
    return copied
