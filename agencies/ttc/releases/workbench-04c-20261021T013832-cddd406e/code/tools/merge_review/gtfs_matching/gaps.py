"""Pure, conservative joint-observation gap segmentation before GTFS matching.

Spatial quality and direction labels deliberately do not affect record presence.
This module never synthesizes an AVL hold or edits an observation. APC intervals
are presence evidence, not invented trajectory points. A single source record
can be inherited from several direction parents; conservation is per parent.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import Counter
from copy import deepcopy
from datetime import datetime

DEFAULTS = {"segmentation_joint_gap_s": 1800.0}


def policy(config=None):
    """Return the recorded semantics and the effective strict gap threshold."""
    threshold = (config or {}).get(
        "segmentation_joint_gap_s", DEFAULTS["segmentation_joint_gap_s"]
    )
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(threshold)
        or threshold < 0
    ):
        raise ValueError("segmentation_joint_gap_s must be finite and nonnegative")
    return {
        "segmentation_joint_gap_s": float(threshold),
        "operator": ">",
        "scope": "each existing same-vehicle direction parent, after route scoping",
        "presence": (
            "union of actual AVL instants and closed APC arrival/departure intervals"
        ),
        "avl_hold_s": 0.0,
        "apc_invalid_interval": "valid arrival point, else valid departure point",
        "timestamp_alias": "AVL event_time only when the time field is absent",
        "direction_labels": "all labels, including unknown and conflicting labels",
        "spatial_quality": "irrelevant to timestamped record presence",
        "invalid_time": (
            "missing, nonfinite, ambiguous or nonexistent cannot bridge gaps"
        ),
        "parent_clipping": True,
        "edge_gaps_split": False,
        "unlocated": "separate diagnostic child only when its parent is split",
        "source_conservation": "exactly once within each original parent membership",
    }


def _clock(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return None
        value = (value - datetime(1970, 1, 1)).total_seconds()
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _key(row):
    return row["source"], row["rid"]


def _refs(keys):
    return [dict(source=source, rid=rid) for source, rid in sorted(set(keys))]


def _presence(row, parent):
    """Actual, reliable, parent-clipped presence; no direction/spatial filtering."""
    if row.get("time_ambiguous") or row.get("time_nonexistent"):
        return None
    if row.get("source") == "avl":
        value = row.get("time") if "time" in row else row.get("event_time")
        left = right = _clock(value)
    elif row.get("source") == "apc":
        left, right = _clock(row.get("arrival")), _clock(row.get("departure"))
        if left is None:
            left = right
        elif right is None or right < left:
            right = left
    else:
        return None
    if left is None or right is None:
        return None
    start, end = _clock(parent.get("start")), _clock(parent.get("end"))
    if start is not None:
        if right < start or (
            right == start and not parent.get("start_inclusive", True)
        ):
            return None
        left = max(left, start)
    if end is not None:
        if left > end or (left == end and not parent.get("end_inclusive", True)):
            return None
        right = min(right, end)
    return (left, right) if left <= right else None


def _gaps(observations, parent, threshold):
    intervals = []
    for index, row in enumerate(observations):
        span = _presence(row, parent)
        if span is not None:
            intervals.append((*span, _key(row), index))
    intervals.sort(key=lambda value: (value[0], value[1], value[2], value[3]))
    components = []
    for left, right, key, _ in intervals:
        if not components or left > components[-1]["end"]:
            components.append(dict(start=left, end=right, left={key}, right={key}))
            continue
        component = components[-1]
        if left == component["start"]:
            component["left"].add(key)
        if right > component["end"]:
            component.update(end=right, right={key})
        elif right == component["end"]:
            component["right"].add(key)
    gaps = []
    for previous, following in zip(components, components[1:], strict=False):
        duration = following["start"] - previous["end"]
        if duration > threshold:
            gaps.append(
                dict(
                    start=previous["end"],
                    end=following["start"],
                    duration_s=duration,
                    threshold_s=threshold,
                    operator=">",
                    left_source_rids=_refs(previous["right"]),
                    right_source_rids=_refs(following["left"]),
                )
            )
    return gaps, intervals


def _membership_fields(rows, parent):
    spans = [_presence(row, parent) for row in rows]
    spans = [span for span in spans if span is not None]
    apc = [row for row in rows if row["source"] == "apc"]
    avl_count = sum(row["source"] == "avl" for row in rows)
    conflicts = sorted(
        {item for row in rows for item in row.get("direction_conflict_ids", []) or []}
    )
    return dict(
        source_start=min((span[0] for span in spans), default=None),
        source_end=max((span[1] for span in spans), default=None),
        source_rids=[dict(source=row["source"], rid=row["rid"]) for row in rows],
        apc_rids=[row["rid"] for row in apc],
        avl_count=avl_count,
        avl_member_count=avl_count,
        apc_count=len(apc),
        conflict_ids=conflicts,
        conflict_count=len(conflicts),
        direction_conflict_count=len(conflicts),
    )


def split_direction_gaps(members, catalog, config=None):
    """Partition existing parent memberships at joint blank gaps strictly > threshold.

    Return ``(new_members, new_catalog, audit)``. Untouched parent IDs remain stable;
    split child IDs are ``parent:gap:0001`` etc. Raw rows are copied unchanged.
    """
    semantics = policy(config)
    threshold = semantics["segmentation_joint_gap_s"]
    parents = {parent["id"]: parent for parent in catalog}
    if len(parents) != len(catalog) or set(parents) != set(members):
        raise ValueError(
            "Gap segmentation requires unique catalog IDs matching members"
        )
    new_members, new_catalog, audit_gaps = {}, [], []
    split_count = unlocated_count = 0
    for parent in catalog:
        parent_id = parent["id"]
        rows = members[parent_id]
        gaps, intervals = _gaps(rows, parent, threshold)
        if not gaps:
            new_members[parent_id] = deepcopy(rows)
            new_catalog.append(deepcopy(parent))
            continue
        split_count += 1
        audit_gaps.extend(
            dict(parent_case_id=parent_id, **deepcopy(gap)) for gap in gaps
        )
        located = {item[3] for item in intervals}
        partitions = [[] for _ in range(len(gaps) + 1)]
        for left, _, _, index in intervals:
            partitions[bisect_right([gap["end"] for gap in gaps], left)].append(index)
        for index, indices in enumerate(partitions):
            child_id = f"{parent_id}:gap:{index + 1:04d}"
            child_rows = [deepcopy(rows[i]) for i in sorted(indices)]
            child = deepcopy(parent)
            child.update(
                id=child_id,
                parent_case_id=parent_id,
                parent_segment_start=parent.get("start"),
                parent_segment_end=parent.get("end"),
                parent_boundary_reason=parent.get("boundary_reason"),
                parent_first_definite_at=deepcopy(parent.get("first_definite_at")),
                parent_boundary_evidence=deepcopy(parent.get("boundary_evidence", [])),
                parent_previous_direction_evidence=deepcopy(
                    parent.get("previous_direction_evidence", [])
                ),
                parent_boundary_preserved=False,
                gap_split=True,
                gap_unlocated=False,
                gap_fragment_index=index + 1,
                gap_fragment_count=len(partitions),
                gap_boundary_before=deepcopy(gaps[index - 1]) if index else None,
                gap_boundary_after=deepcopy(gaps[index]) if index < len(gaps) else None,
                start=gaps[index - 1]["end"] if index else parent.get("start"),
                end=gaps[index]["start"] if index < len(gaps) else parent.get("end"),
                start_inclusive=True if index else parent.get("start_inclusive", True),
                end_inclusive=True
                if index < len(gaps)
                else parent.get("end_inclusive", True),
                boundary_reason="joint_observation_gap"
                if index
                else parent.get("boundary_reason"),
                first_definite_at=None if index else parent.get("first_definite_at"),
                boundary_evidence=[]
                if index
                else deepcopy(parent.get("boundary_evidence", [])),
                previous_direction_evidence=[]
                if index
                else deepcopy(parent.get("previous_direction_evidence", [])),
                scope_censored_start=False
                if index
                else parent.get("scope_censored_start", False),
                scope_boundary_start=False
                if index
                else parent.get("scope_boundary_start", False),
                scope_censored_end=False
                if index < len(gaps)
                else parent.get("scope_censored_end", False),
                scope_boundary_end=False
                if index < len(gaps)
                else parent.get("scope_boundary_end", False),
                **_membership_fields(child_rows, parent),
            )
            for field in ("unknown_duration_s", "conflict_duration_s"):
                child["parent_" + field] = parent.get(field)
                child[field] = None  # The old interval totals cover the entire parent.
            if index:
                for field in ("previous_definite_at", "boundary_from_direction"):
                    child["parent_" + field] = parent.get(field)
                    child[field] = None
                child["boundary_phase"] = "point"
            new_members[child_id] = child_rows
            new_catalog.append(child)
        unlocated = [deepcopy(row) for i, row in enumerate(rows) if i not in located]
        if unlocated:
            child_id = f"{parent_id}:gap:unlocated"
            child = deepcopy(parent)
            child.update(
                id=child_id,
                kind="direction_unresolved",
                parent_case_id=parent_id,
                parent_segment_start=parent.get("start"),
                parent_segment_end=parent.get("end"),
                parent_boundary_reason=parent.get("boundary_reason"),
                parent_first_definite_at=deepcopy(parent.get("first_definite_at")),
                parent_boundary_evidence=deepcopy(parent.get("boundary_evidence", [])),
                parent_previous_direction_evidence=deepcopy(
                    parent.get("previous_direction_evidence", [])
                ),
                parent_boundary_preserved=False,
                gap_split=True,
                gap_unlocated=True,
                gap_fragment_index=None,
                gap_fragment_count=len(partitions),
                gap_boundary_before=None,
                gap_boundary_after=None,
                start=None,
                end=None,
                boundary_reason="unlocated_timestamp",
                first_definite_at=None,
                boundary_evidence=[],
                previous_direction_evidence=[],
                scope_censored_start=False,
                scope_boundary_start=False,
                scope_censored_end=False,
                scope_boundary_end=False,
                **_membership_fields(unlocated, parent),
            )
            new_members[child_id] = unlocated
            new_catalog.append(child)
            unlocated_count += len(unlocated)
    validation = validate_gap_split(members, catalog, new_members, new_catalog, config)
    audit = dict(
        parent_cases=len(catalog),
        output_cases=len(new_catalog),
        split_parent_cases=split_count,
        gap_count=len(audit_gaps),
        unlocated_count=unlocated_count,
        gaps=audit_gaps,
        policy=semantics,
        conservation_violations=validation["violations"],
        validation=validation,
    )
    return new_members, new_catalog, audit


def validate_gap_split(members, catalog, new_members, new_catalog, config=None):
    """Independently check membership conservation, ordering and boundary evidence.

    This does not trust a child's assigned interval when checking for hidden gaps;
    reliable spans are recomputed against its *original parent* first.
    """
    threshold = policy(config)["segmentation_joint_gap_s"]
    parents = {parent["id"]: parent for parent in catalog}
    children = {child["id"]: child for child in new_catalog}
    violations, grouped = [], {parent_id: [] for parent_id in parents}

    def reject(code, **details):
        violations.append(dict(code=code, **details))

    if len(parents) != len(catalog) or set(parents) != set(members):
        reject("parent_catalog_membership")
    if len(children) != len(new_catalog) or set(children) != set(new_members):
        reject("child_catalog_membership")
    for child in new_catalog:
        parent_id = (
            child.get("parent_case_id") if child.get("gap_split") else child["id"]
        )
        if parent_id not in parents:
            reject("unknown_parent", child_id=child["id"])
            continue
        grouped[parent_id].append(child)
    for parent_id, parent in parents.items():
        original_rows = members.get(parent_id, [])
        child_group = grouped[parent_id]
        original_counts = Counter(_key(row) for row in original_rows)
        output_rows = [
            row for child in child_group for row in new_members.get(child["id"], [])
        ]
        if Counter(_key(row) for row in output_rows) != original_counts:
            reject("source_conservation", parent_case_id=parent_id)
        # Check full row values too, without serializing datetime/nonfinite raw data.
        originals = {}
        for row in original_rows:
            originals.setdefault(_key(row), []).append(row)
        for row in output_rows:
            if not any(row == item for item in originals.get(_key(row), [])):
                reject(
                    "source_mutation_or_migration",
                    parent_case_id=parent_id,
                    source_rid=_key(row),
                )
        expected_gaps, _ = _gaps(original_rows, parent, threshold)
        split_children = [
            child for child in child_group if not child.get("gap_unlocated")
        ]
        if len(split_children) != len(expected_gaps) + 1:
            reject("fragment_count", parent_case_id=parent_id)
        split_children.sort(key=lambda child: child.get("gap_fragment_index") or 0)
        for index, child in enumerate(split_children):
            child_id = child["id"]
            child_rows = new_members.get(child_id, [])
            if expected_gaps:
                expected_before = (
                    expected_gaps[index - 1]
                    if 0 < index <= len(expected_gaps)
                    else None
                )
                expected_after = (
                    expected_gaps[index] if index < len(expected_gaps) else None
                )
                if (
                    child.get("gap_boundary_before") != expected_before
                    or child.get("gap_boundary_after") != expected_after
                ):
                    reject("boundary_evidence", child_id=child_id)
                expected_start = (
                    expected_before["end"] if expected_before else parent.get("start")
                )
                expected_end = (
                    expected_after["start"] if expected_after else parent.get("end")
                )
                if (
                    child.get("start") != expected_start
                    or child.get("end") != expected_end
                ):
                    reject("fragment_bounds", child_id=child_id)
                if expected_before and child.get("start_inclusive") is not True:
                    reject("internal_start_inclusivity", child_id=child_id)
                if expected_after and child.get("end_inclusive") is not True:
                    reject("internal_end_inclusivity", child_id=child_id)
                if child.get("direction") != parent.get("direction"):
                    reject("parent_direction_changed", child_id=child_id)
                expected_fields = _membership_fields(child_rows, parent)
                if any(
                    child.get(key) != value for key, value in expected_fields.items()
                ):
                    reject("child_membership_fields", child_id=child_id)
            residual_gaps, intervals = _gaps(child_rows, parent, threshold)
            if residual_gaps:
                reject("unsplit_internal_gap", child_id=child_id)
            if expected_gaps and len(intervals) != len(child_rows):
                reject("unreliable_row_in_located_fragment", child_id=child_id)
            start, end = _clock(child.get("start")), _clock(child.get("end"))
            for left, right, _, _ in intervals:
                if (start is not None and left < start) or (
                    end is not None and right > end
                ):
                    reject("row_outside_fragment", child_id=child_id)
        for child in child_group:
            if child.get("gap_unlocated"):
                if not expected_gaps or child.get("kind") != "direction_unresolved":
                    reject("invalid_unlocated_child", child_id=child["id"])
                if any(
                    _presence(row, parent) is not None
                    for row in new_members.get(child["id"], [])
                ):
                    reject("reliable_row_in_unlocated_fragment", child_id=child["id"])
    return dict(
        valid=not violations,
        violations=dict(Counter(item["code"] for item in violations)),
        violation_details=violations,
        parent_cases=len(parents),
        output_cases=len(children),
    )
