"""Installation bundle integrity and device-control semantics, independent of physical hardware."""

import hashlib
import json
import shutil
import tarfile
import zipfile
from dataclasses import asdict, replace
from datetime import timedelta

import pytest

from custom_components.open_energy_planner.protocol import Plan
from open_energy_planner.installation.commands import plan_document
from open_energy_planner.installation.model import InstalledBattery
from open_energy_planner.installation.storage import InstallationStore, document, parse
from open_energy_planner.planning import InputSnapshot
from script.release import package_ha_app, package_release
from tests.app.installation.test_installation import problem, solve
from tests.app.installation.test_installation_app import installation
from tests.app.solar.test_solar import START
from tests.database.support.archive import make_store, read_document


@pytest.mark.parametrize("tag,version", [(None, "0.0.0.dev0"), ("v0.3.0", "0.3.0")])
def test_matched_bundle_round_trip_and_checksums(tmp_path, monkeypatch, tag, version):
    """Tagged and development bundles preserve shared PNG artwork, matched identities and independent artifact checksums."""
    root = tmp_path / "bundle"
    monkeypatch.setattr(package_ha_app.subprocess, "check_output", lambda *args, **kwargs: "")
    manifest = package_release.build(root, revision="a" * 40, build_number="142.2", release_tag=tag)
    assert manifest["version"] == version and manifest["release_tag"] == tag
    assert manifest["source_state"] == "unverified"
    assert b"\r" not in (root / "SHA256SUMS").read_bytes()
    for name, digest in manifest["artifacts"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
    with zipfile.ZipFile(root / "open_energy_planner.zip") as archive:
        names = archive.namelist()
        assert {"manifest.json", "__init__.py", "protocol.py", "LICENSE", "NOTICE"} <= set(names)
        assert not any(".local" in name or "__pycache__" in name for name in names)
        brand_icon = archive.read("brand/icon.png")
        assert json.loads(archive.read("manifest.json"))["version"] == manifest["version"]
        identity = json.loads(archive.read("build-info.json"))
        assert identity == {
            "version": manifest["version"],
            "build": "142.2",
            "revision": "a" * 40,
            "release_tag": tag,
        }
        assert (
            json.loads(
                (
                    root
                    / "ha-app-repository/open-energy-planner/src/open_energy_planner/build-info.json"
                ).read_text()
            )
            == identity
        )
    assert (
        json.loads((root / "ha-app-repository/open-energy-planner/config.yaml").read_text())[
            "version"
        ]
        == manifest["version"]
    )
    with tarfile.open(root / "ha-app-repository.tar.gz") as archive:
        for artwork_filename in ("icon.png", "logo.png"):
            artwork_stream = archive.extractfile(
                "ha-app-repository/open-energy-planner/" + artwork_filename
            )
            assert artwork_stream is not None
            assert artwork_stream.read() == brand_icon
    with pytest.raises(FileExistsError):
        package_release.build(root, revision="a" * 40)


def test_release_builder_rejects_mixed_versions_before_writing(tmp_path, monkeypatch):
    """An integration/app version mismatch must prevent a distributable bundle rather than leaving mixed installable artifacts."""

    root = tmp_path / "sources"
    for relative in (
        "custom_components/open_energy_planner/manifest.json",
        "ha_app/config.yaml",
        "pyproject.toml",
        "custom_components/open_energy_planner/build-info.json",
        "src/open_energy_planner/build-info.json",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(package_release.ROOT / relative, target)
    manifest = root / "custom_components/open_energy_planner/manifest.json"
    body = json.loads(manifest.read_text(encoding="utf-8"))
    body["version"] = "999.0.0"
    manifest.write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setattr(package_release, "ROOT", root)
    with pytest.raises(ValueError, match="must match"):
        package_release.build(tmp_path / "invalid", revision="a" * 40)
    assert not (tmp_path / "invalid").exists()


def test_release_builder_rejects_empty_revision_without_resolving_head(tmp_path, monkeypatch):
    """An explicitly empty revision is invalid even on a dirty tree. HEAD lookup must not mask it."""

    def unexpected_git(*args, **kwargs):
        """Fail if explicit revision validation unexpectedly invokes Git."""
        raise AssertionError("Explicit revision must not be replaced with HEAD")

    monkeypatch.setattr(package_release.subprocess, "check_output", unexpected_git)
    with pytest.raises(ValueError, match="immutable Git revision"):
        package_release.build(tmp_path / "invalid", revision="")
    assert not (tmp_path / "invalid").exists()


@pytest.mark.parametrize(
    "relative",
    [
        "custom_components/open_energy_planner/secret.json",
        "custom_components/open_energy_planner/brand/icon.png",
        "src/secret.py",
        "src/linked_directory",
        "LICENSE",
    ],
)
def test_release_builder_rejects_linked_inputs_before_writing(tmp_path, monkeypatch, relative):
    """Neither artifact may dereference a linked file/directory, even with a valid revision. Reject before creating output."""
    root = tmp_path / "sources"
    for name in (*package_ha_app.APP_INPUTS, "custom_components", "pyproject.toml"):
        source = package_release.ROOT / name
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(
                source, target, ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True
            )
        else:
            shutil.copy2(source, target)
    link = root / relative
    if link.exists():
        link.unlink()
    outside = tmp_path / "outside"
    if relative.endswith("directory"):
        outside.mkdir()
        (outside / "secret.py").write_text("private build-host contents")
    else:
        outside.write_text("private build-host contents")
    link.symlink_to(outside, target_is_directory=outside.is_dir())
    monkeypatch.setattr(package_release, "ROOT", root)
    monkeypatch.setattr(package_ha_app, "ROOT", root)
    with pytest.raises(ValueError, match="symbolic links"):
        package_release.build(tmp_path / "invalid", revision="a" * 40)
    assert not (tmp_path / "invalid").exists()
    if relative.startswith("src/") or relative in {
        "LICENSE",
        "custom_components/open_energy_planner/brand/icon.png",
    }:
        with pytest.raises(ValueError, match="symbolic links"):
            package_ha_app.package(tmp_path / "app-invalid")
        assert not (tmp_path / "app-invalid").exists()


def test_new_capability_preserves_legacy_configuration_evidence(tmp_path):
    """Default fixed-only batteries keep their original configuration hash/files. Enabling following creates a new revision."""
    from open_energy_planner.solar.calculation.model import version

    body = installation(1)
    body["batteries"][0].pop("can_follow_grid")
    body["batteries"][0].pop("energy_entity")
    legacy = asdict(parse(body))
    legacy.pop("additional_grid_power_entities")
    legacy.pop("additional_grid_export_entities")
    for battery in legacy["batteries"]:
        battery.pop("charge_energy_entity")
    legacy["batteries"][0].pop("can_follow_grid")
    legacy["batteries"][0].pop("energy_entity")
    legacy["batteries"][0].pop("recover_reserve")
    expected = version(legacy)
    settings = parse(body)
    assert settings.version == expected
    assert "can_follow_grid" not in document(settings)["batteries"][0]
    store = make_store(tmp_path, InstallationStore)
    store.save(settings)
    original = read_document(tmp_path, "installation", expected + ".json")
    restored = make_store(tmp_path, InstallationStore)
    assert restored.issue is None and restored.current == settings
    changed = replace(settings, batteries=(replace(settings.batteries[0], can_follow_grid=True),))
    assert changed.version != expected
    store.save(changed)
    assert read_document(tmp_path, "installation", expected + ".json") == original
    assert make_store(tmp_path, InstallationStore).current.batteries[0].can_follow_grid
    with pytest.raises(TypeError):
        InstalledBattery(
            "battery", "inv", "sensor.soc", "sensor.power", 1, 1000, 1000, can_follow_grid=1
        )


@pytest.mark.planner_scenario
@pytest.mark.parametrize("permission", [False, True])
@pytest.mark.parametrize("future_minutes", [None, 8])
def test_zero_grid_execution_preserves_optimal_flows_and_costs(permission, future_minutes):
    """Buy 1 kWh at 1 and cover demand at 3: cost is 1. Following requires permission.

    future_minutes=8 bounds commands to eight minutes. None uses the independent
    20-minute quarter grace. Neither case changes the expected horizon flows.
    """
    inputs = problem()
    inputs = replace(inputs, batteries=(replace(inputs.batteries[0], can_follow_grid=permission),))
    result = solve(inputs)
    snapshot = InputSnapshot(
        "test",
        "config",
        START,
        "measured",
        "forecast",
        "price",
        inputs,
        future_valid_until=START + timedelta(minutes=future_minutes) if future_minutes else None,
    )
    # An eight-minute provider deadline beats the independently specified 20-minute quarter grace.
    envelope = plan_document(snapshot, result, result.plan.costs, START)
    assert envelope["valid_until"] == (START + timedelta(minutes=future_minutes or 20)).isoformat()
    parsed = Plan.parse(envelope, "config")
    assert result.plan.costs.cash_cost == pytest.approx(1)
    first, second = parsed.steps
    assert first.command.grid_active is False
    assert first.command.devices[0].mode == "fixed_power"
    assert first.command.devices[0].power_w == -1000
    assert second.command.grid_active is permission
    assert second.command.devices[0].mode == ("follow_grid_target" if permission else "fixed_power")
    assert second.command.devices[0].power_w == (None if permission else 1000)
    assert envelope["expected_flows"][1]["batteries"][0]["discharge_kwh"] == pytest.approx(1)
    assert envelope["independent_costs"]["cash_cost"] == pytest.approx(1)


@pytest.mark.parametrize(
    "grid_active,mode,power",
    [
        (False, "follow_grid_target", None),
        (True, "fixed_power", 0),
        (True, "follow_grid_target", 500),
    ],
)
def test_protocol_rejects_conflicting_grid_and_device_modes(grid_active, mode, power):
    """A follower cannot retain a numeric target, omit the grid goal or conflict with a fixed device mode."""
    from tests.ha_contract.support.commands import command_plan

    envelope = command_plan(START, powers=(None if grid_active else 0,))
    envelope.update(schema=2, device_commands=True)
    envelope["steps"][0]["devices"] = {
        "battery": {"kind": "battery", "mode": mode, "power_w": power}
    }
    with pytest.raises(ValueError):
        Plan.parse(envelope, envelope["config_revision"])


@pytest.mark.parametrize("ignored", ["src/.env", "src/cache.pyc\nsrc/__pycache__/cache.pyc"])
def test_commit_bundle_rejects_ignored_noncache_files(tmp_path, monkeypatch, ignored):
    """A clean Git status cannot authorize ignored credentials. Cache-only noise remains excluded without blocking a release."""

    def git_output(args, **kwargs):
        """Supply Git's separate clean, immutable revision and ignored-input views."""
        if args[1] == "status":
            return ""
        if args[1] == "rev-parse":
            return "a" * 40
        assert args[1:5] == ["ls-files", "--others", "--ignored", "--exclude-standard"]
        assert "src" in args
        return ignored

    monkeypatch.setattr(package_release.subprocess, "check_output", git_output)
    target = tmp_path / "bundle"
    if ignored == "src/.env":
        with pytest.raises(ValueError, match="src/.env"):
            package_release.build(target)
        assert not target.exists()
    else:
        assert package_release.build(target)["source_state"] == "commit"


@pytest.mark.parametrize(
    "untracked", ["src/injected.py", "custom_components/open_energy_planner/injected.json"]
)
def test_commit_bundle_rejects_untracked_inputs_before_packaging(tmp_path, monkeypatch, untracked):
    """CI's committed-source builder already rejects Git's untracked-input status before staging."""

    def git_output(arguments, **kwargs):
        """Return an independent untracked status and reject any later packaging read."""
        assert arguments[1:] == ["status", "--porcelain"]
        return "?? " + untracked + "\n"

    monkeypatch.setattr(package_release.subprocess, "check_output", git_output)
    destination = tmp_path / "bundle"
    with pytest.raises(ValueError, match="Commit source changes"):
        package_release.build(destination, build_number="90.2")
    assert not destination.exists()


@pytest.mark.parametrize(
    "ignored", ["src/.env.private", "ha_app/.env.private", "src/__pycache__/cache.pyc"]
)
def test_direct_app_builder_rejects_ignored_inputs(tmp_path, monkeypatch, ignored):
    """The standalone app builder rejects ignored credentials before staging, while excluded cache files remain permitted."""
    monkeypatch.setattr(package_ha_app.subprocess, "check_output", lambda *args, **kwargs: ignored)
    target = tmp_path / "app"
    if ".env.private" in ignored:
        with pytest.raises(ValueError, match="ignored files"):
            package_ha_app.package(target)
        assert not target.exists()
    else:
        assert package_ha_app.package(target).is_dir()
