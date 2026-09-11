"""Read-only GTFS workbench adapter, with raw experiment-02 lineage preserved."""

from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path

from tools.merge_review.data import (
    DIRECTION_METHOD,
    PROGRESS_DEFINITION,
    ReviewData,
    _coordinates_valid,
    _decorate_selected_progress,
    json_safe,
)
from tools.merge_review.retention import resolve_direction_parent

from . import METHOD, REPRESENTATION
from .overview import (
    MATCHING_MODES,
    MODE_LABELS,
    available_modes,
    build_day_overview,
    mode_entry,
    overview_case,
    validate_matching_mode,
    visual_fragments,
)
from .runner import artifact_key, route_29

WARNING = (
    "Experiment 03: route 29 only, original experiment-02 direction boundaries. "
    "GTFS feed selection is an explicit unreviewed draft pin. Schedule matching "
    "is experimental; no independently calibrated probability or verified "
    "trip identity. "
    "APC is stop-time interval evidence, not fabricated GPS. OFF_REFERENCE remains "
    "in raw inspection but is excluded from schedule distances and delay estimation."
)
GAP_WARNING = WARNING.replace(
    "original experiment-02 direction boundaries",
    "experiment-02 direction candidates subdivided at joint APC/AVL gaps strictly "
    "longer than the configured threshold; these are review candidates, "
    "not verified trips",
)


def _identity(row, source=None):
    return (source or row["source"], str(row["rid"]))


def _overlap(start, end, lower, upper):
    return (
        start is not None
        and end is not None
        and lower is not None
        and upper is not None
        and start <= upper
        and end >= lower
    )


def _row_bounds(row, source):
    keys = ("arrival", "departure") if source == "apc" else ("t", "event_time", "time")
    values = [row[k] for k in keys if isinstance(row.get(k), (int, float))]
    return (min(values), max(values)) if values else (None, None)


def narrow_gap_detail(detail, summary, catalog, context_s):
    """Select persisted child identities, never reinterpret parent annotations.

    Parent direction review loads all parent members and distant conflict witnesses.
    Only explicit child members survive outside this child's padded window. APC
    context uses its actual arrival/departure interval, never a fabricated GPS point.
    """
    detail = copy.deepcopy(detail)
    focus = {_identity(r) for r in summary["source_rids"]}
    if len(focus) != len(summary["source_rids"]):
        raise ValueError("Duplicate source identity in gap child membership")
    available = {_identity(r) for r in detail["observations"] if route_29(r)}
    if not focus <= available:
        raise ValueError("Gap child source membership is absent from its pinned parent")
    start = (
        summary.get("source_start")
        if summary.get("source_start") is not None
        else summary.get("start")
    )
    end = (
        summary.get("source_end")
        if summary.get("source_end") is not None
        else summary.get("end")
    )
    lower = start - context_s if start is not None else None
    upper = end + context_s if end is not None else None
    # The parent's loaded window is already clipped to the original day scope.
    parent_window = detail.get("window", {})
    if lower is not None and parent_window.get("start") is not None:
        lower = max(lower, parent_window["start"])
    if upper is not None and parent_window.get("end") is not None:
        upper = min(upper, parent_window["end"])
    review_ids = {}
    for candidate in catalog:
        for source in candidate.get("source_rids", []):
            review_ids.setdefault(_identity(source), []).append(candidate["id"])
    positions = preserve_source_breaks(detail["avl"])
    for key in ("observations", "avl", "apc"):
        selected = []
        for row in detail[key]:
            source = row["source"] if key == "observations" else key
            identity = _identity(row, source)
            member = identity in focus
            row_start, row_end = _row_bounds(row, source)
            in_window = _overlap(row_start, row_end, lower, upper)
            if not route_29(row) or not (member or in_window):
                continue
            candidate_member = member and not summary.get("gap_unlocated", False)
            row.update(
                in_focus=member,
                is_member=member,
                is_candidate_member=candidate_member,
                is_segment_member=candidate_member,
                in_window=in_window,
                in_core_window=_overlap(row_start, row_end, start, end),
                membership_relation="member"
                if candidate_member
                else "unresolved"
                if member
                else "context",
                relation="candidate_member"
                if candidate_member
                else "unassigned"
                if member
                else "context",
                parent_candidate_segment_ids=copy.deepcopy(
                    row.get("candidate_segment_ids", [])
                ),
                review_candidate_ids=review_ids.get(identity, []),
                is_conflict_context=False,
                is_direction_boundary_context=False,
            )
            if source == "avl":
                row.update(
                    segment_id=summary["id"] if candidate_member else None,
                    is_witness=False,
                )
            if source == "apc":
                row.update(progress_m=None, status="not_evaluated")
                if "decision" in row:
                    row["decision"]["review_candidate_ids"] = row[
                        "review_candidate_ids"
                    ]
                    row["decision"]["membership_basis"] = (
                        "persisted_gap_child_source_rids"
                    )
            selected.append(row)
        detail[key] = selected
    _decorate_selected_progress(detail["avl"], positions)
    if {_identity(r) for r in detail["observations"] if r["in_focus"]} != focus:
        raise ValueError("Gap child source conservation failed")
    for source in ("avl", "apc"):
        if {_identity(r, source) for r in detail[source] if r["in_focus"]} != {
            identity for identity in focus if identity[0] == source
        }:
            raise ValueError("Gap child visualization lost a source member")
    detail["parent_direction_conflicts"] = copy.deepcopy(
        detail.get("direction_conflicts", [])
    )
    conflicts = []
    for original in detail.get("direction_conflicts", []):
        if not _overlap(original.get("start"), original.get("end"), start, end):
            continue
        if not any(_identity(r) in focus for r in original.get("source_rids", [])):
            continue
        conflict = copy.deepcopy(original)
        conflict.update(
            parent_start=original["start"],
            parent_end=original["end"],
            start=max(start, original["start"]),
            end=min(end, original["end"]),
            clipped_to_child_window=True,
        )
        conflicts.append(conflict)
    visible_conflicts = {c["id"] for c in conflicts}
    for key in ("observations", "avl", "apc"):
        for row in detail[key]:
            row["parent_direction_conflict_ids"] = copy.deepcopy(
                row.get("direction_conflict_ids", [])
            )
            row["direction_conflict_ids"] = [
                c
                for c in row.get("direction_conflict_ids", [])
                if c in visible_conflicts
            ]
            row["is_conflict_context"] = (
                bool(row["direction_conflict_ids"]) and not row["in_focus"]
            )
    detail.update(
        direction_conflicts=conflicts,
        window={
            "start": lower,
            "end": upper,
            "basis": "child_observed_source_coverage",
            "core_start": start,
            "core_end": end,
        },
        gap_membership_basis=(
            "persisted child source_rids; candidate_segment_ids "
            "remain original parent annotations"
        ),
    )
    return detail


def read(path):
    if str(path).endswith(".gz"):
        with gzip.open(path, "rt") as stream:
            return json.load(stream)
    return json.loads(Path(path).read_text())


def projection_point(row):
    status = row.get("projection_status", row.get("status"))
    return {
        **row,
        "status": "VALID" if status == "ON_REFERENCE" else status,
        "s": row.get("s_m"),
        "projection_distance_m": row.get("projection_distance_m", row.get("lateral_m")),
    }


def preserve_source_breaks(rows):
    """Carry parent PI/PO/GPS breaks through a second route-only selection."""
    positions = {}
    position = 0
    for row in rows:
        position += 2 if row.get("break_before") else 1
        positions[row["rid"]] = position
    return positions


def public_gtfs(artifact, canonical, matching_mode="global"):
    """Stable browser aliases; do not infer a second trajectory from raw APC."""
    from .retrieval import for_reference

    entry = mode_entry(artifact, matching_mode)
    projection = copy.deepcopy(artifact["projection"])
    projection["points"] = [projection_point(r) for r in projection.get("points", [])]
    by_reference = {}
    for ref in canonical["references"]:
        trace = for_reference(
            artifact["projection"].get("points", []),
            ref["reference_id"],
            include_unprojectable=True,
        )
        # Use exactly the scorer's continuity rule; this does not reproject points.
        groups = visual_fragments(trace, artifact["config"])
        for row in trace:
            row["fragment_id"] = None
        for index, group in enumerate(groups):
            for row in group:
                row["fragment_id"] = f"{ref['reference_id']}:scoring-fragment:{index}"
        by_reference[ref["reference_id"]] = [projection_point(r) for r in trace]
    projection["by_reference"] = by_reference
    result = copy.deepcopy(entry["result"])
    candidates = result.get("candidates", [])
    for c in candidates:
        c["candidate_id"] = c.get(
            "candidate_id",
            c.get(
                "trip_key",
                f"{c.get('feed_version')}/{c.get('service_date')}/{c.get('trip_id')}",
            ),
        )
        trajectory = c.get("trajectory", c.get("scheduled_trajectory", []))
        knots = []
        for row in trajectory:
            s = row.get("s_m", row.get("s"))
            if "arrival_t" in row or "departure_t" in row:
                for kind, key in (
                    ("arrival", "arrival_t"),
                    ("departure", "departure_t"),
                ):
                    if row.get(key) is not None:
                        knots.append({**row, "t": row[key], "s": s, "kind": kind})
            elif row.get("t") is not None:
                knots.append({**row, "s": s})
        c["trajectory"] = sorted(
            knots, key=lambda r: (r["t"], r.get("stop_sequence", 0))
        )
    references = [
        {**r, "geometry": [{**p, "s": p.get("s_m")} for p in r.get("points", [])]}
        for r in canonical["references"]
    ]
    apc = []
    for row in artifact.get("apc_evidence", []):
        intervals = row.get("canonical_stop_intervals", [])
        if not intervals:
            apc.append({**row, "status": row.get("projection_status"), "s": None})
        for interval in intervals:
            a = interval.get("s_min_m", interval.get("s_m"))
            b = interval.get("s_max_m", a)
            alternative = next(
                (
                    p
                    for p in row.get("projection_alternatives", [])
                    if p["reference_id"] == interval["reference_id"]
                ),
                {},
            )
            status = alternative.get("projection_status", "UNPROJECTABLE")
            s = (
                a
                if a == b
                and not interval.get("ambiguous")
                and status in {"ON_REFERENCE", "STOP_INTERVAL_ONLY"}
                else None
            )
            apc.append(
                {
                    **row,
                    **interval,
                    "status": status,
                    "s": s,
                    "s_m": s,
                    "arrival": row.get("arrival", row.get("start_time")),
                    "departure": row.get("departure", row.get("end_time")),
                }
            )
    return dict(
        result=result,
        candidates=candidates,
        windows=result.get("windows", []),
        projection=projection,
        apc_evidence=apc,
        references=references,
        off_reference_runs=projection.get("off_reference_runs", []),
        config=artifact["config"],
        canonical_validation=canonical["validations"],
        canonical_diagnostics=canonical["diagnostics"],
        direction_crosswalk=canonical.get("direction_crosswalk", []),
        matching_mode=matching_mode,
        available_matching_modes=available_modes(artifact),
        decision_selection=copy.deepcopy(entry.get("selection", {})),
    )


class GTFSReviewData:
    def __init__(self, run):
        self.run = Path(run).resolve()
        self.manifest = read(self.run / "manifest.json")
        self._hash_cache = {}
        self.output_hashes = read(self.run / "output_sha256.json")
        self._verify_parent()
        for relative, digest in self.output_hashes.items():
            if not relative.startswith(("cases/", "observations/")):
                self._verify_file(self.run / relative, digest)
        self.parent = ReviewData(
            self.direction_parent, verify_dependencies=self._verify_parent
        )
        self.vehicles = {
            r["vehicle_case"]: r["raw_vehicle"]
            for r in read(self.run / "selection.json")
        }
        self.methods = (METHOD,)
        self.canonical = read(self.run / "gtfs" / "canonical.json")
        self._catalog = {
            v: read(self.run / "catalog" / f"{v}.json") for v in self.vehicles
        }
        self._overview_cache = {}

    @property
    def available_matching_modes(self):
        saved = self.manifest.get("available_matching_modes", ["global"])
        return [mode for mode in MATCHING_MODES if mode in saved]

    def _matching_mode(self, mode):
        validate_matching_mode(mode)
        if mode not in self.available_matching_modes:
            raise ValueError(f"此历史版本没有计算 {mode} 匹配模式；请选择四模式版本")
        return mode

    def _case_artifact(self, case_id):
        suffix = self.manifest.get("case_artifact_suffix", ".json")
        relative = f"cases/{artifact_key(case_id)}{suffix}"
        self._verify_file(self.run / relative, self.output_hashes[relative])
        return read(self.run / relative), relative

    def _verify_file(self, path, expected):
        from .feed import file_sha256

        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, expected)
        if self._hash_cache.get(str(path)) != signature:
            if path.is_symlink() or file_sha256(path) != expected:
                raise ValueError(
                    "Pinned source or result changed; rebuild the experiment"
                )
            self._hash_cache[str(path)] = signature

    def _verify_parent(self):
        parent = resolve_direction_parent(
            self.run, self.manifest, verify_file=self._verify_file
        )
        for relative, digest in self.manifest["parent_sha256"].items():
            self._verify_file(parent / relative, digest)
        for key, digest in self.manifest["parent_observations_sha256"].items():
            self._verify_file(parent / "observations" / f"{key}.parquet", digest)
        self.direction_parent = parent
        # A long-lived web process may observe an authorized parent relocation.
        # Replace its legacy reader only after the complete copy passes pins.
        if hasattr(self, "parent") and self.parent.run != parent:
            self.parent = ReviewData(parent, verify_dependencies=self._verify_parent)

    def metadata(self):
        self._verify_parent()
        meta = self.parent.metadata()
        meta.update(
            run_id=self.run.name,
            run_path=str(self.run),
            methods=[METHOD],
            default_method=METHOD,
            representation=REPRESENTATION,
            vehicles=[{"id": k, "label": v} for k, v in self.vehicles.items()],
            default_vehicle=next(
                (k for k, v in self.vehicles.items() if v == "9027"),
                next(iter(self.vehicles), None),
            ),
            summary=read(self.run / "summary.json"),
            config=read(self.run / "config.json"),
            warning=GAP_WARNING
            if any(s.get("gap_split") for rows in self._catalog.values() for s in rows)
            else WARNING,
            route_scope=["29"],
            gtfs_used=True,
            schedule_matching_used=True,
            observation_matching_used=False,
            gtfs_selection=self.manifest["gtfs_selection"],
            probability_calibrated=False,
            gap_segmentation_used=any(
                s.get("gap_split") for rows in self._catalog.values() for s in rows
            ),
            gap_segmentation=self.manifest.get("joint_gap_segmentation", {}),
            available_matching_modes=self.available_matching_modes,
            default_matching_mode="global",
            matching_mode_labels=MODE_LABELS,
        )
        return json_safe(meta)

    def catalog(self, method, vehicle, matching_mode="global"):
        self._verify_parent()
        self._matching_mode(matching_mode)
        if method != METHOD or vehicle not in self.vehicles:
            raise ValueError("Unknown GTFS method or route-29 vehicle")
        cases = copy.deepcopy(self._catalog[vehicle])
        for case in cases:
            summaries = case.get(
                "matching_modes", case.get("matching_mode_summary", {})
            )
            summary = summaries.get(matching_mode)
            if summary is None and matching_mode != "global":
                artifact, _ = self._case_artifact(case["id"])
                entry = mode_entry(artifact, matching_mode)
                result = entry["result"]
                summary = {
                    "gtfs_status": result.get("status", "UNMATCHED"),
                    "gtfs_candidate_count": len(result.get("candidates", [])),
                    "pattern_confidence": result.get("pattern_confidence"),
                    "trip_confidence": result.get("trip_confidence"),
                }
            if summary:
                case.update(copy.deepcopy(summary))
            case["matching_mode"] = matching_mode
        return cases

    def has_case(self, method, vehicle, case_id):
        return (
            method == METHOD
            and vehicle in self.vehicles
            and any(r["id"] == case_id for r in self._catalog[vehicle])
        )

    def case(self, method, vehicle, case_id, context_s=180, matching_mode="global"):
        self._verify_parent()
        self._matching_mode(matching_mode)
        if not self.has_case(method, vehicle, case_id):
            raise ValueError("Unknown route-29 GTFS review case")
        summary = next(r for r in self._catalog[vehicle] if r["id"] == case_id)
        parent_case = summary.get("parent_case_id", case_id)
        if summary["kind"] == "direction_unresolved" and not summary.get(
            "parent_case_id"
        ):
            parent_case = next(
                r["id"]
                for r in self.parent.catalog(DIRECTION_METHOD, vehicle)
                if r["kind"] == "direction_unresolved"
            )
        detail = self.parent.case(DIRECTION_METHOD, vehicle, parent_case, context_s)
        positions = preserve_source_breaks(detail["avl"])
        parent_counts = detail["counts"]
        if summary.get("gap_split"):
            detail = narrow_gap_detail(
                detail, summary, self._catalog[vehicle], context_s
            )
        for key in ("observations", "avl", "apc"):
            detail[key] = [r for r in detail[key] if route_29(r)]
        _decorate_selected_progress(detail["avl"], positions)
        included = {(r["source"], str(r["rid"])) for r in detail["observations"]}
        conflicts = []
        for original in detail.get("direction_conflicts", []):
            conflict = copy.deepcopy(original)
            members = conflict.get("source_rids", [])
            kept = [r for r in members if (r["source"], str(r["rid"])) in included]
            if not kept:
                continue
            conflict.update(
                source_rids=kept,
                excluded_context_count=len(members) - len(kept),
                parent_conflict_preserved=True,
            )
            for key in ("north_source_rids", "south_source_rids"):
                conflict[key] = [
                    r
                    for r in conflict.get(key, [])
                    if (r["source"], str(r["rid"])) in included
                ]
            conflicts.append(conflict)
        artifact, relative = self._case_artifact(case_id)
        gtfs = public_gtfs(artifact, self.canonical, matching_mode)
        gtfs["intermediate_artifact"] = str(self.run / relative)
        projected = {str(r["rid"]): r for r in gtfs["projection"]["points"]}
        for row in detail["avl"]:
            if str(row["rid"]) in projected:
                row["gtfs_projection"] = projected[str(row["rid"])]
                row["projection_status"] = projected[str(row["rid"])]["status"]
        for row in detail["apc"]:
            row.update(reason="APC_GTFS_STOP_INTERVAL_EVIDENCE", progress_m=None)
        all_rows = read(self.run / "scope" / f"{vehicle}.json")
        avl, apc, observations = detail["avl"], detail["apc"], detail["observations"]
        counts = {
            **parent_counts,
            "parent_all_route_counts": parent_counts,
            "observations": len(observations),
            "observation_members": sum(r.get("in_focus", False) for r in observations),
            "observation_context": sum(
                not r.get("in_focus", False) for r in observations
            ),
            "vehicle_observations": all_rows["selected_count"],
            "avl_returned": len(avl),
            "apc_returned": len(apc),
            "avl_focus": sum(r.get("in_focus", False) for r in avl),
            "apc_focus": sum(r.get("in_focus", False) for r in apc),
            "avl_member_count": summary["avl_count"],
            "apc_member_count": summary["apc_count"],
            "context_avl_count": sum(not r.get("in_focus", False) for r in avl),
            "apc_context": sum(not r.get("in_focus", False) for r in apc),
            "window_avl_count": sum(
                r.get(
                    "in_core_window" if summary.get("gap_split") else "in_window", False
                )
                for r in avl
            ),
            "direction_conflicts": len(conflicts),
            "avl_unplottable": sum(
                r.get("t") is None or not _coordinates_valid(r) for r in avl
            ),
            "apc_unplottable": sum(
                _row_bounds(r, "apc")[0] is None or not _coordinates_valid(r)
                for r in apc
            ),
            "avl_sampled_or_omitted": 0,
            "avl_witnesses_outside_window": 0,
            "missing_witness_ping_ids": 0,
            "route_scope": ["29"],
            "excluded_other_route_count": all_rows["excluded_count"],
        }
        detail.update(
            case=copy.deepcopy(summary),
            counts=counts,
            gtfs=gtfs,
            direction_conflicts=conflicts,
            conflicts=copy.deepcopy(conflicts),
            representation=REPRESENTATION,
            gtfs_used=True,
            schedule_matching_used=True,
            warnings=[
                GAP_WARNING if summary.get("gap_split") else WARNING,
                PROGRESS_DEFINITION,
            ],
            scope_audit=all_rows,
            segments=[
                {**copy.deepcopy(s), "in_focus": s["id"] == case_id}
                for s in self._catalog[vehicle]
                if s["id"] == case_id
                or _overlap(
                    s.get("start"),
                    s.get("end"),
                    detail["window"]["start"],
                    detail["window"]["end"],
                )
            ],
            provenance={
                "direction_parent_provenance": detail.get("provenance", {}),
                "run_id": self.run.name,
                "run_path": str(self.run),
                "method": METHOD,
                "direction_parent_run_id": self.parent.run.name,
                "direction_parent_case_id": parent_case,
                "gap_split": bool(summary.get("gap_split")),
                "membership_basis": "persisted_gap_child_source_rids"
                if summary.get("gap_split")
                else "original_direction_candidate",
                "gtfs_selection": self.manifest["gtfs_selection"],
                "gtfs_used": True,
                "gtfs_feed_version": self.manifest["gtfs_feed_version"],
                "gtfs_archive_sha256": self.manifest["gtfs_archive_sha256"],
                "manifest": copy.deepcopy(self.manifest),
                "config": read(self.run / "config.json"),
                "representation": REPRESENTATION,
                "schedule_matching_used": True,
                "observation_matching_used": False,
            },
        )
        if "result" in gtfs:
            result = gtfs["result"]
            detail["case"].update(
                matching_mode=matching_mode,
                gtfs_status=result.get("status", "UNMATCHED"),
                gtfs_candidate_count=len(result.get("candidates", [])),
                pattern_confidence=result.get("pattern_confidence"),
                trip_confidence=result.get("trip_confidence"),
            )
        detail["provenance"]["matching_mode"] = matching_mode
        return json_safe(detail)

    def day_overview(self, matching_mode="global"):
        """Read the publication-time compact index, with a legacy lazy fallback.

        Never loads 306 raw parent details or retains full score matrices in cache.
        New runs publish hash-covered overview files during their existing scan.
        """
        self._verify_parent()
        self._matching_mode(matching_mode)
        if not hasattr(self, "_overview_cache"):
            self._overview_cache = {}
        relative = f"overview/{matching_mode}.json"
        if relative in self.output_hashes:
            self._verify_file(self.run / relative, self.output_hashes[relative])
            if matching_mode not in self._overview_cache:
                payload = read(self.run / relative)
                if (
                    payload.get("matching_mode") != matching_mode
                    or payload.get("run_id") != self.run.name
                ):
                    raise ValueError("全天总览与指定实验版本或匹配模式不一致")
                self._overview_cache[matching_mode] = payload
        else:
            # Checking signatures on subsequent reads preserves tamper detection
            # without decompressing every historical artifact again.
            if matching_mode in self._overview_cache:
                suffix = self.manifest.get("case_artifact_suffix", ".json")
                for rows in self._catalog.values():
                    for case in rows:
                        key = f"cases/{artifact_key(case['id'])}{suffix}"
                        self._verify_file(self.run / key, self.output_hashes[key])
            else:
                records = []
                for vehicle, rows in self._catalog.items():
                    for summary in rows:
                        artifact, _ = self._case_artifact(summary["id"])
                        records.append(
                            overview_case(
                                artifact,
                                summary,
                                vehicle,
                                self.vehicles[vehicle],
                                self.canonical,
                                matching_mode,
                            )
                        )
                self._overview_cache[matching_mode] = json_safe(
                    build_day_overview(
                        records,
                        self.canonical,
                        run_id=self.run.name,
                        day=self.manifest.get("debug_date", "2023-11-06"),
                        matching_mode=matching_mode,
                        available_matching_modes=self.available_matching_modes,
                    )
                )
        return copy.deepcopy(self._overview_cache[matching_mode])


def load_review_data(run):
    from tools.merge_review.data import ReviewData as LegacyReviewData

    manifest = read(Path(run) / "manifest.json")
    return (
        GTFSReviewData(run)
        if manifest.get("representation") == REPRESENTATION
        else LegacyReviewData(run)
    )
