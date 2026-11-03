"""Restore disposable runner workspace ownership before checkout without leaving its filesystem."""

import os
import re
import stat
from pathlib import Path

WORK_ROOT = Path("/var/lib/docker/volumes/oep-ci-runner-work/_data")
MOUNT_INFO = Path("/proc/self/mountinfo")


def workspace_path():
    """Require the configured volume and exact repository workspace without symlinked parents."""
    repository = os.environ.get("OEP_RUNNER_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise RuntimeError("Invalid runner repository")
    if Path(os.environ.get("OEP_RUNNER_WORK_PATH", "")) != WORK_ROOT:
        raise RuntimeError("Unexpected runner work volume")
    name = repository.split("/")[1]
    if name in {".", ".."}:
        raise RuntimeError("Invalid runner repository directory")
    expected = WORK_ROOT / name / name
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", ""))
    if workspace != expected or workspace.resolve() != expected:
        raise RuntimeError("Unexpected or symlinked runner workspace")
    return workspace


def walk_error(error):
    """Fail closed when traversal cannot inspect a directory."""
    raise error


def validate_tree(workspace):
    """Check every mount and device boundary before making any ownership or mode change."""
    for line in MOUNT_INFO.read_text().splitlines():
        fields = line.split()
        if len(fields) < 6:
            raise RuntimeError("Invalid Linux mount metadata")
        mount = Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4]))
        if mount.is_relative_to(workspace):
            raise RuntimeError("Mounted path inside runner workspace")
    device = workspace.stat().st_dev
    for _, directories, files, descriptor in os.fwalk(
        workspace, follow_symlinks=False, onerror=walk_error
    ):
        if os.fstat(descriptor).st_dev != device:
            raise RuntimeError("Filesystem boundary inside runner workspace")
        for name in directories + files:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISLNK(metadata.st_mode) and metadata.st_dev != device:
                raise RuntimeError("Filesystem boundary inside runner workspace")
            if not stat.S_ISDIR(metadata.st_mode) and metadata.st_nlink > 1:
                raise RuntimeError("Hard-linked file inside runner workspace")
    return device


def restore_workspace(workspace):
    """Give the runner ownership and directory access using anchored descriptors, never symlink targets."""
    if not workspace.exists():
        return
    if not workspace.is_dir():
        raise RuntimeError("Runner workspace must be a directory")
    device = validate_tree(workspace)
    import pwd

    account = pwd.getpwnam("runner")
    for _, _, files, descriptor in os.fwalk(workspace, follow_symlinks=False, onerror=walk_error):
        metadata = os.fstat(descriptor)
        if metadata.st_dev != device:
            raise RuntimeError("Filesystem boundary inside runner workspace")
        os.fchown(descriptor, account.pw_uid, account.pw_gid)
        os.fchmod(descriptor, stat.S_IMODE(metadata.st_mode) | stat.S_IRWXU)
        for name in files:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                continue
            if metadata.st_dev != device:
                raise RuntimeError("Filesystem boundary inside runner workspace")
            os.chown(name, account.pw_uid, account.pw_gid, dir_fd=descriptor, follow_symlinks=False)


def main():
    """Run only from the image-owned privileged job hook and stop the job on any failure."""
    try:
        if os.geteuid() != 0:
            raise RuntimeError("Workspace preparation requires the image-owned privileged hook")
        restore_workspace(workspace_path())
    except (OSError, RuntimeError, KeyError) as error:
        raise SystemExit(f"Runner workspace preparation failed: {error}") from None


if __name__ == "__main__":
    main()
