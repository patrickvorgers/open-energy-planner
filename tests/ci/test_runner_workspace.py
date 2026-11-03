"""Runner preparation must repair generated artifacts without changing resources outside its workspace."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from script.ci import runner_workspace


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    """Create a disposable repository layout independent of any live Docker volume."""
    work_root = tmp_path / "work"
    directory = work_root / "planner" / "planner"
    directory.mkdir(parents=True)
    monkeypatch.setattr(runner_workspace, "WORK_ROOT", work_root)
    monkeypatch.setenv("OEP_RUNNER_REPOSITORY", "owner/planner")
    monkeypatch.setenv("OEP_RUNNER_WORK_PATH", str(work_root))
    monkeypatch.setenv("GITHUB_WORKSPACE", str(directory))
    mount_info = tmp_path / "mountinfo"
    mount_info.write_text("1 0 0:1 / / rw - tmpfs tmpfs rw\n")
    monkeypatch.setattr(runner_workspace, "MOUNT_INFO", mount_info)
    return directory


def test_only_exact_repository_workspace_is_accepted(workspace):
    """The configured owner repository maps to one exact disposable workspace."""
    assert runner_workspace.workspace_path() == workspace


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("OEP_RUNNER_REPOSITORY", "owner/../../outside"),
        ("OEP_RUNNER_REPOSITORY", ""),
        ("OEP_RUNNER_WORK_PATH", "/unrelated-volume"),
        ("GITHUB_WORKSPACE", "/unrelated-repository"),
        ("GITHUB_WORKSPACE", ""),
    ],
)
def test_invalid_context_is_rejected_before_repair(monkeypatch, workspace, variable, value):
    """Missing metadata, traversal and foreign directories cannot enter privileged preparation."""
    monkeypatch.setenv(variable, value)
    with pytest.raises(RuntimeError):
        runner_workspace.workspace_path()


@pytest.mark.parametrize("name", [".", ".."])
def test_repository_cannot_alias_the_work_volume(monkeypatch, workspace, name):
    """Dot repository names cannot broaden repair to the volume or its parents."""
    monkeypatch.setenv("OEP_RUNNER_REPOSITORY", f"owner/{name}")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(runner_workspace.WORK_ROOT))
    with pytest.raises(RuntimeError, match="repository directory"):
        runner_workspace.workspace_path()


@pytest.mark.skipif(os.name != "posix", reason="Linux runner filesystem semantics")
def test_symlinked_workspace_is_rejected_without_changing_target(workspace, tmp_path):
    """A repository-directory symlink cannot turn workspace cleanup into an outside change."""
    workspace.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    original = outside.stat()
    workspace.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlinked"):
        runner_workspace.workspace_path()
    assert outside.stat() == original


@pytest.mark.parametrize("relative", ["", "foreign volume", "file"])
def test_nested_mount_rejected_before_any_mutation(workspace, relative):
    """Both file and directory mount points reject preparation before ownership changes."""
    mount = str(workspace / relative).replace("\\", "\\134").replace(" ", "\\040")
    runner_workspace.MOUNT_INFO.write_text(f"1 0 0:2 / {mount} rw - tmpfs tmpfs rw\n")
    original = workspace.stat()
    with pytest.raises(RuntimeError, match="Mounted path"):
        runner_workspace.restore_workspace(workspace)
    assert workspace.stat() == original


def test_device_boundary_rejected_before_any_mutation(monkeypatch, workspace):
    """An unexpected device discovered during the complete first traversal prevents all changes."""
    original = workspace.stat()
    descriptor_marker = object()
    original_fstat = os.fstat
    monkeypatch.setattr(
        runner_workspace.os,
        "fwalk",
        lambda *args, **kwargs: iter([(str(workspace), [], [], descriptor_marker)]),
        raising=False,
    )
    monkeypatch.setattr(
        runner_workspace.os,
        "fstat",
        lambda descriptor: (
            SimpleNamespace(st_dev=original.st_dev + 1)
            if descriptor is descriptor_marker
            else original_fstat(descriptor)
        ),
    )
    with pytest.raises(RuntimeError, match="Filesystem boundary"):
        runner_workspace.restore_workspace(workspace)
    assert workspace.stat() == original


@pytest.mark.skipif(os.name != "posix", reason="Linux runner filesystem semantics")
def test_hard_link_rejected_before_changing_shared_inode(workspace, tmp_path):
    """A file sharing an outside inode rejects the whole repair before ownership can leak out."""
    outside = tmp_path / "outside-file"
    outside.write_text("preserved outside inode")
    os.link(outside, workspace / "linked-file")
    os.utime(workspace, ns=(1_000_000_000, workspace.stat().st_mtime_ns))
    original_file = outside.stat()
    original_directory = workspace.stat()
    with pytest.raises(RuntimeError, match="Hard-linked file"):
        runner_workspace.restore_workspace(workspace)
    # Directory reads may update atime on relatime filesystems, but must not mutate anything else.
    unchanged_attributes = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_uid",
        "st_gid",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    for path, original in ((outside, original_file), (workspace, original_directory)):
        actual = path.stat()
        for attribute in unchanged_attributes:
            assert getattr(actual, attribute) == getattr(original, attribute), attribute
    assert outside.read_text() == "preserved outside inode"


@pytest.mark.skipif(os.name != "posix", reason="Linux runner filesystem semantics")
def test_root_artifacts_become_runner_owned_and_symlinks_stay_outside(
    monkeypatch, workspace, tmp_path
):
    """Root-created output and readonly directories become cleanable without modifying symlink targets."""
    import pwd

    if os.geteuid() != 0:
        pytest.skip("Real ownership changes require the isolated root test container")
    monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=1001, pw_gid=1001))
    generated = workspace / "results" / "annual-setup"
    generated.mkdir(parents=True)
    output = generated / "screenshot.png"
    output.write_bytes(b"generated fixture")
    generated.chmod(0o500)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "preserved"
    target.write_text("outside data")
    (workspace / "directory-link").symlink_to(outside, target_is_directory=True)
    (workspace / "file-link").symlink_to(target)
    original = target.stat()
    runner_workspace.restore_workspace(workspace)
    assert output.stat().st_uid == 1001
    assert generated.stat().st_uid == 1001
    assert generated.stat().st_mode & 0o700 == 0o700
    assert generated.stat().st_mode & 0o077 == 0
    assert target.stat() == original
    assert target.read_text() == "outside data"


def test_hook_failure_is_terminal(monkeypatch, workspace):
    """A failed preparation cannot report success and allow checkout to proceed."""
    monkeypatch.setattr(runner_workspace.os, "geteuid", lambda: 0, raising=False)

    def fail(directory):
        """Model an ownership failure after safe path admission."""
        raise OSError("ownership unavailable")

    monkeypatch.setattr(runner_workspace, "restore_workspace", fail)
    with pytest.raises(SystemExit, match="Runner workspace preparation failed"):
        runner_workspace.main()


def test_hook_and_helper_are_image_owned_not_checkout_sourced():
    """The privileged entrypoint and isolated Python helper are copied into the runner image."""
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / ".github/runner/Dockerfile").read_text()
    hook = (root / ".github/runner/job-started.sh").read_text()
    assert (
        "ENV ACTIONS_RUNNER_HOOK_JOB_STARTED=/usr/local/bin/oep-runner-job-started.sh" in dockerfile
    )
    assert "COPY script/ci/runner_workspace.py /usr/local/lib/oep-runner-workspace.py" in dockerfile
    assert "/usr/bin/python3 -I -S /usr/local/lib/oep-runner-workspace.py" in hook
    assert "exec /usr/bin/sudo -n " in hook
