"""Generate a self-contained HA app build context from the same release sources, excluding secrets/test data."""

import argparse
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BRAND_ICON = "custom_components/open_energy_planner/brand/icon.png"
APP_INPUTS = (
    "ha_app",
    "requirements.lock",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    "src",
    "script/runtime/ha_app.py",
    BRAND_ICON,
)


def validate_sources(root, paths):
    """Reject symbolic links in approved inputs before copying any build-host file contents."""
    for name in paths:
        path = root / name
        for parent in (path, *path.parents):
            if parent == root:
                break
            if parent.is_symlink():
                raise ValueError("Release sources must not contain symbolic links")
        if path.is_dir() and any(item.is_symlink() for item in path.rglob("*")):
            raise ValueError("Release sources must not contain symbolic links")


def reject_ignored(root, paths):
    """Reject ignored non-cache inputs before packaging can copy local credentials."""
    ignored = [
        name
        for name in subprocess.check_output(
            ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--", *paths],
            cwd=root,
            text=True,
        ).splitlines()
        if "__pycache__" not in Path(name).parts and not name.endswith(".pyc")
    ]
    if ignored:
        raise ValueError("Remove ignored files from release inputs: " + ", ".join(ignored))


def package(destination):
    """Create a new repository build directory. Never overwrite an existing installation or include local credentials."""
    validate_sources(ROOT, APP_INPUTS)
    reject_ignored(ROOT, APP_INPUTS)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "repository.json").write_text(
        json.dumps(
            {
                "name": "Open Energy Planner",
                "url": "https://github.com/patrickvorgers/open-energy-planner",
                "maintainer": "Open Energy Planner contributors",
            },
            indent=2,
        )
        + "\n"
    )
    app = destination / "open-energy-planner"
    app.mkdir()
    for name in ("config.yaml", "Dockerfile"):
        shutil.copy2(ROOT / "ha_app" / name, app / name)
    for artwork_filename in ("icon.png", "logo.png"):
        shutil.copy2(ROOT / BRAND_ICON, app / artwork_filename)
    for name in ("requirements.lock", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / name, app / name)
    shutil.copytree(
        ROOT / "src", app / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    (app / "script").mkdir()
    shutil.copy2(ROOT / "script" / "runtime" / "ha_app.py", app / "script" / "ha_app.py")
    return app


def main():
    """Write an explicit new release build directory, ready for the app repository publication step."""
    parser = argparse.ArgumentParser()
    parser.add_argument("destination")
    args = parser.parse_args()
    print(package(args.destination))


if __name__ == "__main__":
    main()
