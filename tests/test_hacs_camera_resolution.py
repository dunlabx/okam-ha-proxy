import sys
from pathlib import Path

import importlib.util
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))


_spec = importlib.util.spec_from_file_location(
    "okam_identity", Path(__file__).parents[1] / "custom_components/okam/identity.py"
)
assert _spec and _spec.loader
_identity = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_identity)

CameraSelectionRequired = _identity.CameraSelectionRequired
CameraAliasAmbiguous = _identity.CameraAliasAmbiguous
resolve_reference = _identity.resolve_reference
validated_from_devices = _identity.validated_from_devices


def _devices():
    return [
        {"uid": "UID_FRONT", "camera_uid": "UID_FRONT", "alias": "front door", "name": "Front"},
        {"uid": "UID_CABIN", "camera_uid": "UID_CABIN", "alias": "cabin", "name": "Cabin"},
    ]


def test_hacs_accepts_uid_and_stores_canonical_uid():
    result = validated_from_devices({"bridge_url": "http://bridge", "camera_id": "UID_FRONT"}, _devices())
    assert result["camera_uid"] == "UID_FRONT"
    assert result["camera_id"] == "UID_FRONT"


def test_hacs_accepts_alias_and_stores_canonical_uid():
    result = validated_from_devices({"bridge_url": "http://bridge", "camera_id": "Cabin"}, _devices())
    assert result["camera_uid"] == "UID_CABIN"
    assert result["camera_id"] == "UID_CABIN"
    assert result["camera_alias"] == "cabin"


def test_alias_rename_does_not_change_stored_uid():
    result = validated_from_devices({"camera_uid": "UID_CABIN", "camera_id": "old"}, [
        {"uid": "UID_CABIN", "alias": "new", "name": "Cabin"}
    ])
    assert result["camera_uid"] == "UID_CABIN"


def test_unknown_reference_and_ambiguous_alias_are_rejected():
    with pytest.raises(ValueError, match="camera_not_found"):
        validated_from_devices({"camera_id": "missing"}, _devices())
    with pytest.raises(CameraAliasAmbiguous):
        resolve_reference([
            {"uid": "A", "alias": "same"},
            {"uid": "B", "alias": "SAME"},
        ], "same")


def test_blank_reference_still_requests_selection_for_multiple_cameras():
    with pytest.raises(CameraSelectionRequired):
        validated_from_devices({"camera_id": ""}, _devices())


def test_exact_uid_wins_over_alias_collision():
    selected = resolve_reference([
        {"uid": "same", "alias": "other"},
        {"uid": "B", "alias": "same"},
    ], "same")
    assert selected["uid"] == "same"
