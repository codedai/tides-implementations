"""Destructive maintenance is exercised exclusively inside pytest tmp_path."""

import json

import pytest

from tools.merge_review import cleanup


def put(path, value="evidence"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if isinstance(value, dict) else value)
    return path


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "artifacts"
    put(root / "old" / "case.gz", "old candidate bytes")
    put(root / "old" / "manifest.json", {"run": "old"})
    put(root / "old" / "notes" / "reviews.sqlite3", "human notes")
    put(root / "retained" / "case.gz", "retained candidate bytes")
    raw = put(tmp_path / "raw" / "source.csv", "original raw")
    audit = root / "cleanup-audits" / "test"
    return root, audit, raw


def planned(workspace):
    root, audit, raw = workspace
    cleanup.plan(root, ["old"], [root / "retained", raw], audit)
    return root, audit, raw


def validate(audit):
    checkpoint = put(audit / "checkpoint.json", {"status": "PASS"})
    ui = put(audit / "cold-ui.json", {"status": "PASS", "cold_start": True})
    return cleanup.record_validation(audit, checkpoint, ui)


def test_plan_hashes_every_file_prunes_reviews_and_reports_size(workspace):
    root, audit, _ = planned(workspace)
    result = json.loads((audit / "plan.json").read_text())
    assert set(result["inventory"]) == {"old/case.gz", "old/manifest.json"}
    assert result["bytes"] == sum(
        (root / p).stat().st_size for p in result["inventory"]
    )
    assert all(len(v["sha256"]) == 64 for v in result["inventory"].values())
    assert str(root / "old/notes/reviews.sqlite3") in result["protected"]


def test_stage_restore_preserves_bytes_inodes_and_notes(workspace):
    root, audit, _ = planned(workspace)
    original = cleanup._signature(root / "old/case.gz")
    assert cleanup.stage(audit) == 2
    assert not (root / "old/case.gz").exists()
    assert (root / "old/notes/reviews.sqlite3").read_text() == "human notes"
    cleanup.restore(audit)
    assert cleanup._signature(root / "old/case.gz") == original


def test_empty_staged_run_is_absent_and_restore_recreates_directories(workspace):
    root, audit, raw = workspace
    put(root / "disposable/nested/case.gz", "rebuildable result")
    cleanup.plan(root, ["disposable"], [root / "retained", raw], audit)
    cleanup.stage(audit)
    assert not (root / "disposable").exists()
    cleanup.restore(audit)
    assert (root / "disposable/nested/case.gz").read_text() == "rebuildable result"


def test_purge_requires_validation_and_keeps_metadata_and_reviews(workspace):
    root, audit, raw = planned(workspace)
    cleanup.stage(audit)
    with pytest.raises(FileNotFoundError):
        cleanup.purge(audit)
    checked = validate(audit)
    assert checked["verified_retained_files"] == 3
    result = cleanup.purge(audit)
    assert result["status"] == "PURGED" and result["files"] == 2
    assert not (audit / "quarantine").exists()
    assert (audit / "retained-metadata/old/manifest.json").is_file()
    assert not (audit / "retained-metadata/old/case.gz").exists()
    assert (root / "old/notes/reviews.sqlite3").read_text() == "human notes"
    assert raw.read_text() == "original raw"
    assert (root / "retained/case.gz").read_text() == "retained candidate bytes"


@pytest.mark.parametrize("target", [".", "../outside", "retained", "raw_audit"])
def test_unsafe_or_protected_targets_rejected(workspace, target):
    root, audit, raw = workspace
    put(root / "raw_audit/source.csv")
    with pytest.raises(ValueError):
        cleanup.plan(root, [target], [root / "retained", raw], audit)
    assert not audit.exists()


def test_ancestor_of_raw_or_protected_rejected(workspace):
    root, audit, raw = workspace
    put(root / "old/raw_audit/source.csv")
    with pytest.raises(ValueError, match="Raw data"):
        cleanup.plan(root, ["old"], [root / "retained", raw], audit)


@pytest.mark.parametrize("location", ["target", "descendant", "parent"])
def test_symlinks_rejected(workspace, tmp_path, location):
    root, audit, raw = workspace
    if location == "descendant":
        (root / "old/link").symlink_to(raw)
        target = "old"
    elif location == "target":
        (root / "link").symlink_to(root / "old", target_is_directory=True)
        target = "link"
    else:
        (tmp_path / "linked").symlink_to(root, target_is_directory=True)
        root, target = tmp_path / "linked", "old"
    with pytest.raises(ValueError, match="Symlink"):
        cleanup.plan(root, [target], [raw], audit)


@pytest.mark.parametrize("change", ["edited", "foreign"])
def test_target_changes_before_stage_block_all_moves(workspace, change):
    root, audit, _ = planned(workspace)
    put(root / "old" / ("case.gz" if change == "edited" else "new.txt"), "changed")
    with pytest.raises(ValueError, match="inventory changed"):
        cleanup.stage(audit)
    assert (root / "old/manifest.json").exists()


def test_plan_tampering_rejected(workspace):
    _, audit, _ = planned(workspace)
    put(audit / "plan.json", {})
    with pytest.raises(ValueError, match="plan changed"):
        cleanup.stage(audit)


@pytest.mark.parametrize("change", ["protected", "quarantine", "foreign", "report"])
def test_post_validation_changes_prevent_any_purge(workspace, change):
    root, audit, _ = planned(workspace)
    cleanup.stage(audit)
    validate(audit)
    target = {
        "protected": root / "retained/case.gz",
        "quarantine": audit / "quarantine/old/case.gz",
        "foreign": audit / "quarantine/foreign.txt",
        "report": audit / "cold-ui.json",
    }[change]
    put(target, "changed")
    with pytest.raises(ValueError):
        cleanup.purge(audit)
    assert (audit / "quarantine/old/manifest.json").is_file()
    assert not (audit / "purge-started.json").exists()


def test_warm_or_failed_ui_report_not_accepted(workspace):
    _, audit, _ = planned(workspace)
    cleanup.stage(audit)
    checkpoint = put(audit / "checkpoint.json", {"status": "PASS"})
    ui = put(audit / "cold-ui.json", {"status": "PASS", "cold_start": False})
    with pytest.raises(ValueError, match="ui_cold_check"):
        cleanup.record_validation(audit, checkpoint, ui)


def test_restore_never_overwrites_new_file(workspace):
    root, audit, _ = planned(workspace)
    cleanup.stage(audit)
    put(root / "old/case.gz", "new result")
    with pytest.raises(ValueError, match="overwrite"):
        cleanup.restore(audit)
    assert (root / "old/case.gz").read_text() == "new result"
    assert (audit / "quarantine/old/case.gz").is_file()


def test_protected_new_file_and_new_review_detected(workspace):
    root, audit, _ = planned(workspace)
    put(root / "new-reviews.sqlite3", "new human note")
    with pytest.raises(ValueError, match="Protected or review files changed"):
        cleanup.stage(audit)


def test_targets_cannot_overlap(workspace):
    root, audit, raw = workspace
    with pytest.raises(ValueError, match="Overlapping"):
        cleanup.plan(root, ["old", "old/case.gz"], [raw], audit)


def test_restore_after_protected_change_still_recovers_old_files(workspace):
    root, audit, _ = planned(workspace)
    cleanup.stage(audit)
    put(root / "retained/case.gz", "user edit")
    cleanup.restore(audit)
    assert (root / "old/case.gz").read_text() == "old candidate bytes"
    assert (root / "retained/case.gz").read_text() == "user edit"


def test_staging_error_rolls_back_already_moved_files(workspace, monkeypatch):
    root, audit, _ = planned(workspace)
    original = type(root).rename

    def broken(path, target):
        if path.name == "manifest.json" and "quarantine" in target.parts:
            raise OSError("injected rename failure")
        return original(path, target)

    monkeypatch.setattr(type(root), "rename", broken)
    with pytest.raises(OSError, match="injected"):
        cleanup.stage(audit)
    assert (root / "old/case.gz").read_text() == "old candidate bytes"
    assert (root / "old/manifest.json").exists()
    assert not (audit / "staged.json").exists()
