"""Unit tests for config.load_app_config.

Each test writes a small apps/<name>.yaml under a tmp_path "project root" so
tests never touch (or depend on the contents of) the real apps/ directory,
except for the two tests that explicitly load the committed sample configs.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from config import ConfigError, load_app_config, validate_dependencies_for

VALID_ANDROID_YAML = textwrap.dedent(
    """\
    name: "test-android-app"
    platform: "android"
    repo: "acme/test-android-app"
    clone_path: "workspace/test-android-app"
    flows_dir: "flows/test-android-app"
    reference_dir: "references/test-android-app"

    device:
      android:
        avd_name: "pixel_7_api_34"
        api_level: 34

    build:
      android:
        module: ":app"
        build_command: "./gradlew :app:assembleDebug"
        artifact_path: "app/build/outputs/apk/debug/app-debug.apk"
        install_command: "adb install -r {artifact_path}"
        package_id: "com.example.testapp"
    """
)

VALID_IOS_YAML = textwrap.dedent(
    """\
    name: "test-ios-app"
    platform: "ios"
    repo: "acme/test-ios-app"
    clone_path: "workspace/test-ios-app"
    flows_dir: "flows/test-ios-app"
    reference_dir: "references/test-ios-app"

    device:
      ios:
        simulator_name: "iPhone 15"
        os_version: "17.5"

    build:
      ios:
        scheme: "TestIOSApp"
        workspace: "TestIOSApp.xcworkspace"
        build_command: "xcodebuild -workspace TestIOSApp.xcworkspace -scheme TestIOSApp"
        artifact_path: "build/Debug-iphonesimulator/TestIOSApp.app"
        install_command: "xcrun simctl install booted {artifact_path}"
        bundle_id: "com.example.testiosapp"
    """
)


def _write_app(root: Path, app_name: str, content: str) -> None:
    apps_dir = root / "apps"
    apps_dir.mkdir(parents=True, exist_ok=True)
    (apps_dir / f"{app_name}.yaml").write_text(content)


def test_flows_dir_falls_back_to_clone_path_when_missing_under_project(tmp_path: Path) -> None:
    clone = tmp_path / "app-checkout"
    flows = clone / "flows/app"
    flows.mkdir(parents=True)
    yaml_text = VALID_ANDROID_YAML.replace(
        'clone_path: "workspace/test-android-app"',
        f'clone_path: "{clone}"',
    ).replace(
        'flows_dir: "flows/test-android-app"',
        'flows_dir: "flows/app"',
    )
    _write_app(tmp_path, "clone_flows", yaml_text)

    cfg = load_app_config("clone_flows", project_root=tmp_path)
    assert cfg.flows_dir == flows.resolve()


def test_load_valid_android_config(tmp_path: Path) -> None:
    _write_app(tmp_path, "test_android", VALID_ANDROID_YAML)

    cfg = load_app_config("test_android", project_root=tmp_path)

    assert cfg.name == "test-android-app"
    assert cfg.platform == "android"
    assert cfg.repo == "acme/test-android-app"
    assert cfg.clone_path == (tmp_path / "workspace/test-android-app").resolve()
    assert cfg.flows_dir == (tmp_path / "flows/test-android-app").resolve()
    assert cfg.reference_dir == (tmp_path / "references/test-android-app").resolve()
    assert cfg.device.avd_name == "pixel_7_api_34"
    assert cfg.build.package_id == "com.example.testapp"
    assert cfg.app_id == "com.example.testapp"
    assert cfg.build.test_command is None  # optional field, not set in VALID_ANDROID_YAML
    assert cfg.regression_flows == []  # optional field, defaults to empty
    assert cfg.base_branch == "develop"


def test_optional_test_command_is_picked_up_when_present(tmp_path: Path) -> None:
    with_test_command = VALID_ANDROID_YAML.replace(
        'package_id: "com.example.testapp"',
        'package_id: "com.example.testapp"\n    test_command: "./gradlew test"',
    )
    _write_app(tmp_path, "with_test_command", with_test_command)

    cfg = load_app_config("with_test_command", project_root=tmp_path)

    assert cfg.build.test_command == "./gradlew test"


def test_optional_regression_flows_is_picked_up_when_present(tmp_path: Path) -> None:
    with_regression_flows = VALID_ANDROID_YAML + '\nregression_flows:\n  - "smoke_test.yaml"\n  - "checkout_flow.yaml"\n'
    _write_app(tmp_path, "with_regression_flows", with_regression_flows)

    cfg = load_app_config("with_regression_flows", project_root=tmp_path)

    assert cfg.regression_flows == ["smoke_test.yaml", "checkout_flow.yaml"]


def test_load_valid_ios_config(tmp_path: Path) -> None:
    _write_app(tmp_path, "test_ios", VALID_IOS_YAML)

    cfg = load_app_config("test_ios", project_root=tmp_path)

    assert cfg.name == "test-ios-app"
    assert cfg.platform == "ios"
    assert cfg.device.simulator_name == "iPhone 15"
    assert cfg.build.bundle_id == "com.example.testiosapp"
    assert cfg.app_id == "com.example.testiosapp"


def test_missing_top_level_field(tmp_path: Path) -> None:
    broken = VALID_ANDROID_YAML.replace('repo: "acme/test-android-app"\n', "")
    _write_app(tmp_path, "broken", broken)

    with pytest.raises(ConfigError, match="repo"):
        load_app_config("broken", project_root=tmp_path)


def test_empty_top_level_field_treated_as_missing(tmp_path: Path) -> None:
    broken = VALID_ANDROID_YAML.replace(
        'repo: "acme/test-android-app"', 'repo: ""'
    )
    _write_app(tmp_path, "broken_empty", broken)

    with pytest.raises(ConfigError, match="repo"):
        load_app_config("broken_empty", project_root=tmp_path)


def test_unsupported_platform(tmp_path: Path) -> None:
    broken = VALID_ANDROID_YAML.replace('platform: "android"', 'platform: "windows"')
    _write_app(tmp_path, "unsupported", broken)

    with pytest.raises(ConfigError, match="unsupported platform"):
        load_app_config("unsupported", project_root=tmp_path)


def test_missing_platform_specific_block(tmp_path: Path) -> None:
    data = yaml.safe_load(VALID_ANDROID_YAML)
    del data["device"]["android"]
    _write_app(tmp_path, "no_device_block", yaml.safe_dump(data))

    with pytest.raises(ConfigError, match="device.android"):
        load_app_config("no_device_block", project_root=tmp_path)


def test_missing_platform_specific_field(tmp_path: Path) -> None:
    data = yaml.safe_load(VALID_ANDROID_YAML)
    del data["build"]["android"]["package_id"]
    _write_app(tmp_path, "no_package_id", yaml.safe_dump(data))

    with pytest.raises(ConfigError, match="package_id"):
        load_app_config("no_package_id", project_root=tmp_path)


def test_wrong_platform_block_present_but_not_selected_platform(tmp_path: Path) -> None:
    # An android app that only has an "ios" device/build block should fail
    # with a clear "device.android is required" error, not a KeyError.
    data = yaml.safe_load(VALID_ANDROID_YAML)
    data["device"] = {"ios": {"simulator_name": "iPhone 15", "os_version": "17.5"}}
    _write_app(tmp_path, "wrong_block", yaml.safe_dump(data))

    with pytest.raises(ConfigError, match="device.android"):
        load_app_config("wrong_block", project_root=tmp_path)


def test_malformed_yaml(tmp_path: Path) -> None:
    _write_app(tmp_path, "malformed", "name: [unclosed\nplatform: android")

    with pytest.raises(ConfigError, match="Malformed YAML"):
        load_app_config("malformed", project_root=tmp_path)


def test_yaml_is_not_a_mapping(tmp_path: Path) -> None:
    _write_app(tmp_path, "not_a_mapping", "- just\n- a\n- list\n")

    with pytest.raises(ConfigError, match="mapping"):
        load_app_config("not_a_mapping", project_root=tmp_path)


def test_config_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_app_config("does_not_exist", project_root=tmp_path)


def test_invalid_repo_format(tmp_path: Path) -> None:
    broken = VALID_ANDROID_YAML.replace(
        'repo: "acme/test-android-app"', 'repo: "not-a-valid-repo"'
    )
    _write_app(tmp_path, "bad_repo", broken)

    with pytest.raises(ConfigError, match="at least one slash"):
        load_app_config("bad_repo", project_root=tmp_path)


def test_nested_gitlab_repo_path(tmp_path: Path) -> None:
    nested = VALID_ANDROID_YAML.replace(
        'repo: "acme/test-android-app"',
        'repo: "allios/touchpoint/android/b2b-android"',
    )
    _write_app(tmp_path, "nested_repo", nested)
    cfg = load_app_config("nested_repo", project_root=tmp_path)
    assert cfg.repo == "allios/touchpoint/android/b2b-android"


def test_dependency_cycle_rejected(tmp_path: Path) -> None:
    _write_app(tmp_path, "app_a", VALID_ANDROID_YAML.replace('name: "test-android-app"', 'name: "a"'))
    b_yaml = VALID_ANDROID_YAML.replace('name: "test-android-app"', 'name: "b"').replace(
        'repo: "acme/test-android-app"', 'repo: "acme/b"'
    )
    _write_app(tmp_path, "app_b", b_yaml + '\ndependencies:\n  - app: "app_a"\n')
    a_yaml = (tmp_path / "apps" / "app_a.yaml").read_text() + '\ndependencies:\n  - app: "app_b"\n'
    (tmp_path / "apps" / "app_a.yaml").write_text(a_yaml)
    with pytest.raises(ConfigError, match="cycle"):
        validate_dependencies_for("app_a", project_root=tmp_path)




def test_sample_android_config_loads() -> None:
    cfg = load_app_config("sample_android")
    assert cfg.platform == "android"
    assert cfg.app_id == "com.example.sampleandroidapp"


def test_sample_ios_config_loads() -> None:
    cfg = load_app_config("sample_ios")
    assert cfg.platform == "ios"
    assert cfg.app_id == "com.example.sampleiosapp"


def test_loading_config_never_executes_a_shell_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(*args, **kwargs):  # pragma: no cover - should never run
        raise AssertionError("load_app_config must not execute subprocess commands")

    monkeypatch.setattr(subprocess, "run", _fail)
    monkeypatch.setattr(subprocess, "Popen", _fail)
    monkeypatch.setattr("os.system", _fail)

    load_app_config("sample_android")
    load_app_config("sample_ios")
