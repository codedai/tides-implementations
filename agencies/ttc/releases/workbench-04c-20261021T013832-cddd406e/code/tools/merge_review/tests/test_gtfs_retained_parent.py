"""The visual-debug rerun path must survive verified old-run retention."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.merge_review import direction_trips
from tools.merge_review.gtfs_matching import REPRESENTATION
from tools.merge_review.gtfs_matching.runner import direction_parent
from tools.merge_review.tests.test_retention import (
    build_retention_bundle,
    bundle,
    frozen,  # noqa: F401
    sha,
    write,
)


@pytest.fixture
def retained_parent(request):
    fixture = request.getfixturevalue("frozen")
    parent = json.loads((fixture.parent / "manifest.json").read_text())
    parent["representation"] = direction_trips.REPRESENTATION
    write(fixture.parent / "manifest.json", parent)
    fixture.manifest["representation"] = REPRESENTATION
    fixture.manifest["parent_sha256"]["manifest.json"] = sha(
        fixture.parent / "manifest.json"
    )
    write(fixture.consumer / "manifest.json", fixture.manifest)
    write(
        fixture.consumer / "output_sha256.json",
        {
            name: sha(fixture.consumer / name)
            for name in ("manifest.json", "config.json")
        },
    )

    def get_run(run_id):
        path = fixture.workspace / "runs" / run_id
        completion = path / "completion.json"
        if path.is_symlink() or not completion.is_file():
            raise ValueError("Run absent or incomplete")
        if not json.loads(completion.read_text()).get("validation_ok"):
            raise ValueError("Run did not complete validation")
        return path

    workspace = SimpleNamespace(
        root=fixture.workspace, date="2023-11-06", get_run=get_run
    )
    return fixture, workspace


def test_existing_direction_and_gtfs_parents_keep_the_original_lookup(
    retained_parent, monkeypatch
):
    fixture, workspace = retained_parent
    from tools.merge_review import retention

    def forbidden(*args, **kwargs):
        raise AssertionError("An existing parent should use workspace.get_run")

    monkeypatch.setattr(retention, "resolve_direction_parent", forbidden)
    assert direction_parent(workspace, fixture.parent.name)[0] == fixture.parent
    assert direction_parent(workspace, fixture.consumer.name)[0] == fixture.parent


def test_deleted_original_is_resolved_only_through_verified_checkpoint(retained_parent):
    fixture, workspace = retained_parent
    before = (fixture.consumer / "manifest.json").read_bytes()
    _, checkpoint = bundle(fixture)
    selected, manifest = direction_parent(workspace, fixture.consumer.name)
    assert selected == checkpoint
    assert manifest["run_id"] == fixture.parent.name
    assert manifest["representation"] == direction_trips.REPRESENTATION
    assert (fixture.consumer / "manifest.json").read_bytes() == before


@pytest.mark.parametrize(
    "relative",
    ["manifest.json", "direction/vehicle-a.json", "observations/vehicle-b.parquet"],
)
def test_tampered_checkpoint_cannot_be_a_new_experiment_parent(
    retained_parent, relative
):
    fixture, workspace = retained_parent
    _, checkpoint = bundle(fixture)
    (checkpoint / relative).write_bytes(b"modified")
    with pytest.raises(ValueError, match="changed"):
        direction_parent(workspace, fixture.consumer.name)


def test_unregistered_consumer_cannot_borrow_deleted_parent(retained_parent):
    fixture, workspace = retained_parent
    bundle(fixture)
    unknown = fixture.workspace / "runs" / "unknown-consumer"
    write(unknown / "manifest.json", fixture.manifest)
    write(unknown / "completion.json", {"validation_ok": True})
    with pytest.raises(ValueError, match="Unknown retention consumer"):
        direction_parent(workspace, unknown.name)


def test_missing_parent_without_checkpoint_is_rejected(retained_parent):
    fixture, workspace = retained_parent
    fixture.parent.rename(fixture.quarantine)
    with pytest.raises(ValueError, match="Cannot read retention dependency"):
        direction_parent(workspace, fixture.consumer.name)


def test_existing_incomplete_parent_never_falls_back_to_good_checkpoint(
    retained_parent,
):
    fixture, workspace = retained_parent
    build_retention_bundle(fixture.workspace, [fixture.consumer], fixture.parent)
    write(fixture.parent / "completion.json", {"validation_ok": False})
    with pytest.raises(ValueError, match="did not complete validation"):
        direction_parent(workspace, fixture.consumer.name)


def test_dangling_original_symlink_is_not_a_missing_parent(retained_parent):
    fixture, workspace = retained_parent
    bundle(fixture)
    fixture.parent.symlink_to(fixture.workspace / "missing", target_is_directory=True)
    with pytest.raises(ValueError, match="absent or incomplete"):
        direction_parent(workspace, fixture.consumer.name)


def test_next_generation_rerun_can_point_directly_to_live_checkpoint(retained_parent):
    fixture, workspace = retained_parent
    _, checkpoint = bundle(fixture)
    next_run = fixture.workspace / "runs" / "next-rerun"
    next_manifest = copy.deepcopy(fixture.manifest)
    next_manifest.update(run_id=next_run.name, direction_parent_path=str(checkpoint))
    write(next_run / "manifest.json", next_manifest)
    write(next_run / "completion.json", {"validation_ok": True})
    selected, _ = direction_parent(workspace, next_run.name)
    assert selected == Path(checkpoint)
