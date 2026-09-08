import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]

# This is the schema-element grammar used by Home Assistant Supervisor's app
# validator.  Keeping the grammar here makes the test exercise the parsed
# add-on schema rather than asserting on source text.
SUPERVISOR_SCHEMA_ELEMENT = re.compile(
    r"^(?:"
    r"|bool|email|url|port"
    r"|device(?:\(subsystem=[a-z]+\))?"
    r"|str(?:\(\d+?,\d+?\))?"
    r"|password(?:\(\d+?,\d+?\))?"
    r"|int(?:\(-?\d+?,-?\d+?\))?"
    r"|float(?:\(-?\d*\.?\d+,-?\d*\.?\d+\))?"
    r"|match\(.*\)"
    r"|list\(.+\)"
    r")\??$"
)


def _assert_supervisor_schema_element(value: object, path: str) -> None:
    """Validate schema values using Supervisor's supported YAML shape."""
    if isinstance(value, str):
        assert SUPERVISOR_SCHEMA_ELEMENT.fullmatch(value), (path, value)
    elif isinstance(value, list):
        assert len(value) == 1, (path, value)
        _assert_supervisor_schema_element(value[0], f"{path}[]")
    elif isinstance(value, dict):
        for key, child in value.items():
            assert isinstance(key, str), (path, key)
            _assert_supervisor_schema_element(child, f"{path}.{key}")
    else:
        raise AssertionError(f"{path} has unsupported schema value {value!r}")


def _load_addon_config(relative_path: str) -> dict[str, object]:
    value = yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_fork_repository_and_addon_identity_are_local() -> None:
    repository = yaml.safe_load(
        (ROOT / "repository.yaml").read_text(encoding="utf-8")
    )
    config = _load_addon_config("okam_native_app/config.yaml")
    assert repository == {
        "name": "O-KAM HA Proxy",
        "url": "https://github.com/dunlabx/okam-ha-proxy",
        "maintainer": "dunlabx",
    }
    assert config["name"] == "O-KAM HA Proxy"
    assert config["slug"] == "okam_ha_proxy"
    assert config["url"] == "https://github.com/dunlabx/okam-ha-proxy"
    assert config["image"] == "ghcr.io/dunlabx/okam-ha-proxy"


def test_addon_config_matches_supervisor_schema_expectations() -> None:
    config = _load_addon_config("okam_native_app/config.yaml")
    required = {"name", "version", "slug", "description", "arch"}
    assert required <= config.keys()
    assert isinstance(config["name"], str) and config["name"]
    assert isinstance(config["version"], str) and config["version"]
    assert re.fullmatch(r"[a-z0-9_]+", str(config["slug"]))
    assert isinstance(config["description"], str) and config["description"]
    assert config["arch"] == ["aarch64", "amd64"]
    assert config["startup"] == "application"
    assert config["boot"] == "auto"
    assert config["ports"] == {"8099/tcp": 8099, "8100/tcp": 8100}
    assert set(config["ports_description"]) == set(config["ports"])
    assert config["image"] == "ghcr.io/dunlabx/okam-ha-proxy"

    options = config["options"]
    schema = config["schema"]
    assert isinstance(options, dict)
    assert isinstance(schema, dict)
    assert set(options) <= set(schema)
    assert options["cameras"] == []
    assert schema["cameras"] == [{"uid": "str", "alias": "str?"}]
    for key, value in schema.items():
        _assert_supervisor_schema_element(value, f"schema.{key}")


def test_test_addon_config_matches_supervisor_schema_expectations() -> None:
    config = _load_addon_config("okam_native_app/test-addon/config.yaml")
    assert re.fullmatch(r"[a-z0-9_]+", str(config["slug"]))
    assert config["options"]["cameras"] == []
    assert config["schema"]["cameras"] == [{"uid": "str", "alias": "str?"}]
    for key, value in config["schema"].items():
        _assert_supervisor_schema_element(value, f"schema.{key}")


def test_hacs_repository_layout_and_manifest_are_installable() -> None:
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (ROOT / "custom_components" / "okam" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert isinstance(hacs.get("name"), str) and hacs["name"]
    assert set(manifest) >= {
        "domain",
        "documentation",
        "issue_tracker",
        "codeowners",
        "name",
        "version",
    }
    assert manifest["domain"] == "okam"
    assert manifest["version"] == "1.2.4"
    assert manifest["documentation"].startswith("https://github.com/dunlabx/")
    assert manifest["issue_tracker"].startswith("https://github.com/dunlabx/")
    integration_dirs = sorted(
        path.name
        for path in (ROOT / "custom_components").iterdir()
        if path.is_dir() and path.name != "__pycache__"
    )
    assert integration_dirs == ["okam"]
    icon = ROOT / "custom_components" / "okam" / "brand" / "icon.png"
    assert icon.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_native_image_excludes_windows_gui_runtime() -> None:
    dockerfile = (ROOT / "okam_native_app" / "Dockerfile").read_text(encoding="utf-8")
    for forbidden in ("wine", "box64", "webviewer", "xvfb", "libgtk"):
        assert forbidden not in dockerfile.lower()
    assert "ffmpeg" in dockerfile.lower()
    assert "LIBHYBRIS_COMMIT=7079712a42ea2754adf747e70c6cc75764c8596e" in dockerfile
    assert "AOSP_SHA1=e209114dd0dfc2f4e0d328f5fd7367fec39ee1bd" in dockerfile
    assert "runtime-amd64" in dockerfile
    assert "runtime-arm64" in dockerfile
    assert "FROM runtime-${TARGETARCH} AS final" in dockerfile


def test_status_distinguishes_loader_from_camera_acceptance() -> None:
    entrypoint = (ROOT / "okam_native_app" / "app_entrypoint.py").read_text(
        encoding="utf-8"
    )
    assert '"loader_ready": False' in entrypoint
    assert '"account_ready": False' in entrypoint
    assert '"p2p_ready": False' in entrypoint
    assert '"camera_ready": False' in entrypoint
    assert "p2p_connected=true clean_disconnect=true" in entrypoint
    assert "camera_authenticated=true clean_disconnect=true" in entrypoint
    assert "h264_received=true" in entrypoint
    assert "snapshot_created=true" in entrypoint
    assert 'RUNTIME_ARCH == "amd64"' in entrypoint
    assert 'RUNTIME_ARCH == "aarch64"' in entrypoint
    assert "/opt/okam/okam-amd64-connect" in entrypoint
    assert 'command.append("--wake-only")' in entrypoint
    assert "build_candidates" in entrypoint
    assert "camera_auth_cache.json" in entrypoint
    assert "CameraAuthenticator" in entrypoint
    assert "camera device credential was unavailable" not in entrypoint


def test_publish_workflow_builds_one_multi_architecture_image() -> None:
    workflow = (ROOT / ".github" / "workflows" / "publish-image.yml").read_text(
        encoding="utf-8"
    )
    assert "platform: linux/arm64" in workflow
    assert "platform: linux/amd64" in workflow
    assert "BUILD_ARCH=${{ matrix.ha_arch }}" in workflow
    assert "TARGETARCH=${{ matrix.docker_arch }}" in workflow
    assert "docker buildx imagetools create" in workflow
    assert '$VERSION-aarch64"' in workflow
    assert '$VERSION-amd64"' in workflow
    assert workflow.count("docker/build-push-action@v6") == 1
    assert "-candidate-${GITHUB_SHA}-" in workflow
    assert "Promote the exact architecture artifacts" in workflow


def test_repository_contains_camera_integration_for_native_api() -> None:
    component = ROOT / "custom_components" / "okam"
    manifest = (component / "manifest.json").read_text(encoding="utf-8")
    config_flow = (component / "config_flow.py").read_text(encoding="utf-8")
    integration_init = (component / "__init__.py").read_text(encoding="utf-8")
    strings = (component / "strings.json").read_text(encoding="utf-8")
    camera = (component / "camera.py").read_text(encoding="utf-8")
    assert '"version": "1.2.4"' in manifest
    assert "http://homeassistant.local:8099" in config_flow
    assert "CameraEntityFeature.STREAM" in camera
    assert "_attr_has_entity_name = False" in camera
    assert "SLEEPING_PLACEHOLDER" in camera
    assert "WAKING_PLACEHOLDER" in camera
    assert "WAKE_WATCH_SECONDS = 90" in camera
    assert "self.async_update_token()" in camera
    assert "self._last_snapshot" in camera
    assert (
        "self.internal_integration_suggested_object_id = runtime.coordinator.camera_id"
        in camera
    )
    assert "CONF_CAMERA_UID" in config_flow
    assert "SelectSelector" in config_flow
    assert "camera_uid" in strings
    assert "async_update_entry" in integration_init
    assert "type OkamConfigEntry =" not in integration_init
    assert "async_step_camera" in config_flow
    assert "async_step_reconfigure_camera" in config_flow
    assert "CameraSelectionRequired" in config_flow
    assert "f\"{bridge_url}:{camera_uid}\"" in config_flow
    assert "_camera_label(item, uid)" in config_flow
    assert 'for key in ("alias", "name", "camera_id")' in config_flow
    assert "The configured camera UID is no longer available" in integration_init
    assert "uid(item).casefold() == configured_uid.strip().casefold()" in integration_init
    assert "multiple cameras; reconfigure this entry" in integration_init
    assert "/api/cameras/{camera_uid}/status" in (ROOT / "custom_components" / "okam" / "api.py").read_text(encoding="utf-8")


def test_integration_uses_two_minute_warm_connection_default() -> None:
    constants = (ROOT / "custom_components" / "okam" / "const.py").read_text(
        encoding="utf-8"
    )
    assert "DEFAULT_IDLE_TIMEOUT = 120" in constants


def test_user_documentation_is_current_and_complete() -> None:
    documents = [
        ROOT / "README.md",
        ROOT / "SECURITY.md",
        ROOT / "docs" / "architecture.md",
        ROOT / "docs" / "troubleshooting.md",
        ROOT / "okam_native_app" / "DOCS.md",
        ROOT / "okam_native_app" / "README.md",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in documents)
    lowered = combined.lower()
    assert "experimental" not in lowered
    assert "okam-ha-arm64" not in lowered
    assert "webviewer" not in lowered
    assert "windows" not in lowered
    assert "wine" not in lowered
    assert "box64" not in lowered
    assert "xvfb" not in lowered
    assert "https://github.com/dunlabx/okam-ha-proxy" in combined
    assert "custom_components/okam" in combined
    assert "camera.cabin" in combined
    assert "secondary" not in lowered
    assert "aarch64" in lowered
    assert "amd64" in lowered


def test_image_refuses_a_runtime_stage_that_mismatches_the_platform() -> None:
    # A build that omits TARGETARCH selects the amd64 runtime whatever the
    # platform, which would ship an arm64 image with no official transport.
    dockerfile = (ROOT / "okam_native_app" / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM runtime-${TARGETARCH} AS final" in dockerfile
    assert "does not match platform" in dockerfile
    assert 'aarch64) expected=arm64' in dockerfile


def test_publish_workflow_passes_the_runtime_stage_selector() -> None:
    workflow = (ROOT / ".github" / "workflows" / "publish-image.yml").read_text(
        encoding="utf-8"
    )

    assert "TARGETARCH=${{ matrix.docker_arch }}" in workflow
    assert "docker_arch: arm64" in workflow
    assert "docker_arch: amd64" in workflow
    assert "ghcr.io/dunlabx/okam-ha-proxy" in workflow
    assert "ghcr.io/oleandor/okam-ha-native" not in workflow


def test_image_ffmpeg_can_mux_the_live_stream() -> None:
    # The advertised stream source is MPEG-TS. Home Assistant's stream worker
    # rejects a raw elementary stream with "No dts in N consecutive packets",
    # and the minimal FFmpeg build only has the muxers it is told to keep.
    dockerfile = (ROOT / "okam_native_app" / "Dockerfile").read_text(encoding="utf-8")

    assert "--enable-muxer=mpegts" in dockerfile
    assert "--enable-muxer=image2pipe" in dockerfile
    assert "--enable-demuxer=h264" in dockerfile


def test_latest_tag_only_ever_follows_main() -> None:
    # A test build published from a branch must not repoint the tag that
    # installations track.
    workflow = (ROOT / ".github" / "workflows" / "publish-image.yml").read_text(
        encoding="utf-8"
    )

    assert 'GITHUB_REF}" = "refs/heads/main"' in workflow
    assert 'if [ "$MOVE_LATEST" = "true" ]' in workflow
    assert "tag_suffix" in workflow


def test_test_addon_cannot_collide_with_the_installed_one() -> None:
    installed = (ROOT / "okam_native_app" / "config.yaml").read_text(encoding="utf-8")
    candidate = (
        ROOT / "okam_native_app" / "test-addon" / "config.yaml"
    ).read_text(encoding="utf-8")

    assert "slug: okam_ha_proxy\n" in installed
    assert "slug: okam_ha_proxy_test\n" in candidate
    # Distinct host port, so both can run side by side.
    assert "8099/tcp: 8099" in installed
    assert "8099/tcp: 8098" in candidate
    assert "8100/tcp: 8100" in installed
    assert "8100/tcp: 8101" in candidate
    assert "boot: manual" in candidate


def test_camera_password_is_optional_in_both_addon_schemas() -> None:
    # The bridge falls back to the enumerated credential when this is empty,
    # but a schema entry without a trailing "?" is required, so the supervisor
    # refused to save the options at all. There is no safe placeholder either:
    # any value here overrides the credential the account hands back.
    for name in ("config.yaml", "test-addon/config.yaml"):
        config = (ROOT / "okam_native_app" / name).read_text(encoding="utf-8")
        assert "  camera_password: password?\n" in config, name
        # An optional option validates when the key is absent. A null default
        # keeps it present and invalid, which the supervisor reports as the
        # option being missing.
        assert "  camera_password: null\n" not in config, name


def test_watchdog_targets_a_declared_container_port() -> None:
    # [PORT:n] names the container port, which the supervisor resolves to the
    # host port. Pointing it at the host side matches nothing, the watchdog
    # never resolves, and the app is restarted underneath a live stream.
    import re

    for name in ("config.yaml", "test-addon/config.yaml"):
        config = (ROOT / "okam_native_app" / name).read_text(encoding="utf-8")
        watched = re.search(r"watchdog:.*\[PORT:(\d+)\]", config)
        assert watched is not None, name
        declared = set(re.findall(r"^  (\d+)/tcp:", config, re.MULTILINE))
        assert watched.group(1) in declared, name


def test_addon_changelog_matches_the_repository_changelog() -> None:
    # The supervisor only shows release notes from a CHANGELOG.md inside the
    # app directory, so it is a copy. Keep the copy honest.
    root = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    packaged = (ROOT / "okam_native_app" / "CHANGELOG.md").read_text(encoding="utf-8")

    assert packaged == root
