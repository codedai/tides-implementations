"""Validated, version/route/direction-isolated reference families in metres."""

from __future__ import annotations

import collections
import copy
import hashlib
import math

DEFAULTS = {
    "canonical_error_m": 30.0,
    "canonical_ambiguity_delta_m": 10.0,
    "canonical_ambiguity_progress_m": 75.0,
    "canonical_max_ambiguity_fraction": 0.02,
    "canonical_backtrack_m": 10.0,
    "canonical_sample_m": 40.0,
    "canonical_min_direction_displacement_m": 300.0,
}
R = 6371008.8


def finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def xy(lat, lon, origin):
    return (
        math.radians(lon - origin[1]) * R * math.cos(math.radians(origin[0])),
        math.radians(lat - origin[0]) * R,
    )


def valid_coordinate(lat, lon):
    return (
        finite(lat)
        and finite(lon)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
        and (lat, lon) != (0, 0)
    )


def geometry(points, origin):
    output, distance = [], 0.0
    for point in points:
        lat, lon = point["lat"], point["lon"]
        if not valid_coordinate(lat, lon):
            raise ValueError("Invalid coordinate in GTFS shape")
        x, y = xy(lat, lon, origin)
        if output:
            distance += math.hypot(x - output[-1]["x_m"], y - output[-1]["y_m"])
        output.append(dict(lat=lat, lon=lon, x_m=x, y_m=y, s_m=distance))
    if distance == 0:
        raise ValueError("Zero-length GTFS reference")
    return output


def make_index(reference, cell_m=100.0):
    grid = collections.defaultdict(set)
    for index, (a, b) in enumerate(
        zip(reference["points"], reference["points"][1:], strict=False)
    ):
        x0, x1 = sorted((int(a["x_m"] // cell_m), int(b["x_m"] // cell_m)))
        y0, y1 = sorted((int(a["y_m"] // cell_m), int(b["y_m"] // cell_m)))
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                grid[x, y].add(index)
    return dict(grid=grid, cell_m=cell_m)


def project_candidates(lat, lon, reference, *, gate=None, top_k=6, index=None):
    """Segment projections, retaining separated progress hypotheses at loops."""
    if not valid_coordinate(lat, lon):
        return []
    x, y = xy(lat, lon, reference["origin"])
    points = reference["points"]
    indices = range(len(points) - 1)
    if gate is not None and index is not None:
        cell = index["cell_m"]
        found = set()
        for gx in range(int((x - gate) // cell), int((x + gate) // cell) + 1):
            for gy in range(int((y - gate) // cell), int((y + gate) // cell) + 1):
                found.update(index["grid"].get((gx, gy), ()))
        indices = sorted(found)
    candidates = []
    for segment in indices:
        a, b = points[segment], points[segment + 1]
        dx, dy = b["x_m"] - a["x_m"], b["y_m"] - a["y_m"]
        length2 = dx * dx + dy * dy
        if length2 == 0:
            continue
        fraction = min(
            1.0, max(0.0, ((x - a["x_m"]) * dx + (y - a["y_m"]) * dy) / length2)
        )
        px, py = a["x_m"] + fraction * dx, a["y_m"] + fraction * dy
        lateral = math.hypot(x - px, y - py)
        if gate is not None and lateral > gate:
            continue
        candidates.append(
            dict(
                s_m=a["s_m"] + fraction * math.sqrt(length2),
                lateral_m=lateral,
                segment_index=segment,
                heading_deg=math.degrees(math.atan2(dx, dy)) % 360,
                projected_lat=a["lat"] + fraction * (b["lat"] - a["lat"]),
                projected_lon=a["lon"] + fraction * (b["lon"] - a["lon"]),
            )
        )
    candidates.sort(key=lambda c: (c["lateral_m"], c["s_m"], c["segment_index"]))
    distinct = []
    for candidate in candidates:
        if not any(abs(candidate["s_m"] - old["s_m"]) < 3 for old in distinct):
            distinct.append(candidate)
            if len(distinct) >= top_k:
                break
    return distinct


def _sample(points, spacing):
    kept = [points[0]]
    for point in points[1:-1]:
        if point["s_m"] - kept[-1]["s_m"] >= spacing:
            kept.append(point)
    return kept + [points[-1]]


def _mapping(source, target, cfg):
    samples = _sample(source["points"], cfg["canonical_sample_m"])
    positions, residuals, ambiguous = [], [], 0
    index = make_index(target)
    for point in samples:
        candidates = project_candidates(
            point["lat"],
            point["lon"],
            target,
            gate=cfg["canonical_error_m"],
            index=index,
        )
        if not candidates:
            return dict(
                accepted=False,
                reason="geometry_outside_tolerance",
                samples=len(samples),
            )
        best = candidates[0]
        ambiguous += any(
            abs(other["s_m"] - best["s_m"]) > cfg["canonical_ambiguity_progress_m"]
            and other["lateral_m"] - best["lateral_m"]
            <= cfg["canonical_ambiguity_delta_m"]
            for other in candidates[1:]
        )
        positions.append(best["s_m"])
        residuals.append(best["lateral_m"])
    backwards = sum(
        b < a - cfg["canonical_backtrack_m"]
        for a, b in zip(positions, positions[1:], strict=False)
    )
    fraction = ambiguous / len(samples)
    accepted = backwards == 0 and fraction <= cfg["canonical_max_ambiguity_fraction"]
    return dict(
        accepted=accepted,
        reason="validated_monotone_geometry"
        if accepted
        else "ambiguous_or_nonmonotone",
        samples=len(samples),
        max_error_m=max(residuals),
        mean_error_m=sum(residuals) / len(residuals),
        ambiguous_fraction=fraction,
        backtracks=backwards,
        sample_mapping=[
            dict(shape_s_m=p["s_m"], canonical_s_m=s)
            for p, s in zip(samples, positions, strict=True)
        ],
    )


def _stop_path(times, stops, reference, cfg):
    """Monotone stop-order DP; loops retain positional ambiguity diagnostics."""
    layers, output = [], []
    index = make_index(reference)
    for stop in times:
        raw = stops[stop["stop_id"]]
        candidates = project_candidates(
            raw["lat"],
            raw["lon"],
            reference,
            gate=cfg["canonical_error_m"],
            top_k=12,
            index=index,
        )
        if not candidates:
            return None
        layers.append(candidates)
    if not layers:
        return []
    costs = [c["lateral_m"] ** 2 for c in layers[0]]
    back = []
    for previous, current in zip(layers, layers[1:], strict=False):
        next_cost, parents = [], []
        for candidate in current:
            choices = [
                (costs[j], j)
                for j, old in enumerate(previous)
                if candidate["s_m"] >= old["s_m"] - cfg["canonical_backtrack_m"]
            ]
            cost, parent = min(choices, default=(math.inf, None))
            next_cost.append(cost + candidate["lateral_m"] ** 2)
            parents.append(parent)
        back.append(parents)
        costs = next_cost
    if not costs or not math.isfinite(min(costs)):
        return None
    selected = min(range(len(costs)), key=lambda i: (costs[i], i))
    path = [selected]
    for parents in reversed(back):
        selected = parents[selected]
        path.append(selected)
    for stop, layer, selected in zip(times, layers, reversed(path), strict=True):
        best = layer[selected]
        ambiguous = any(
            abs(c["s_m"] - best["s_m"]) > cfg["canonical_ambiguity_progress_m"]
            and c["lateral_m"] <= best["lateral_m"] + cfg["canonical_ambiguity_delta_m"]
            for c in layer
        )
        output.append(
            dict(
                stop, s_m=best["s_m"], lateral_m=best["lateral_m"], ambiguous=ambiguous
            )
        )
    return output


def build_references(bundle, config=None):
    """Prefer longest only after low-error, low-ambiguity monotone validation."""
    cfg = DEFAULTS | (config or {})
    for key in DEFAULTS:
        if not finite(cfg[key]) or cfg[key] < 0:
            raise ValueError(f"Invalid {key}")
    version, route = bundle["feed_version"], bundle["route_id"]
    stops = {r["stop_id"]: r for r in bundle["stops"]}
    all_points = [p for shape in bundle["shapes"] for p in shape["points"]]
    if not all_points:
        raise ValueError("No shapes used by route trips")
    origin = [
        sum(p["lat"] for p in all_points) / len(all_points),
        sum(p["lon"] for p in all_points) / len(all_points),
    ]
    shapes = {s["shape_id"]: geometry(s["points"], origin) for s in bundle["shapes"]}
    uses = collections.defaultdict(list)
    for trip in bundle["reference_trips"]:
        if trip["route_id"] != route:
            raise ValueError("Reference route namespace contamination")
        uses[trip.get("direction_id", ""), trip["shape_id"]].append(trip)
    candidates = []
    crosswalk = []
    for (direction, shape_id), trips in sorted(uses.items()):
        points = shapes[shape_id]
        northing = points[-1]["y_m"] - points[0]["y_m"]
        geometry_label = (
            "NORTH"
            if northing > cfg["canonical_min_direction_displacement_m"]
            else "SOUTH"
            if northing < -cfg["canonical_min_direction_displacement_m"]
            else None
        )
        easting = points[-1]["x_m"] - points[0]["x_m"]
        east_west = cfg.get("continuous_matching_enabled", False) and abs(
            easting
        ) > abs(northing)
        if east_west:
            geometry_label = (
                ("EAST" if easting > 0 else "WEST")
                if abs(easting) > cfg["canonical_min_direction_displacement_m"]
                else None
            )
        patterns = {tuple(s["stop_id"] for s in t["stop_times"]) for t in trips}
        votes = []
        for pattern in patterns:
            if len(pattern) < 2:
                continue
            delta = (
                math.radians(stops[pattern[-1]]["lat"] - stops[pattern[0]]["lat"]) * R
            )
            if east_west:
                delta = (
                    math.radians(stops[pattern[-1]]["lon"] - stops[pattern[0]]["lon"])
                    * R
                    * math.cos(math.radians(origin[0]))
                )
            if abs(delta) >= cfg["canonical_min_direction_displacement_m"]:
                votes.append(
                    ("EAST" if delta > 0 else "WEST")
                    if east_west
                    else ("NORTH" if delta > 0 else "SOUTH")
                )
        label = (
            geometry_label
            if geometry_label is not None and votes and set(votes) == {geometry_label}
            else None
        )
        crosswalk.append(
            dict(
                feed_version=version,
                route_id=route,
                direction_id=direction,
                shape_id=shape_id,
                direction_label=label,
                geometry_northing_m=northing,
                ordered_stop_direction_votes=dict(collections.Counter(votes)),
                basis=(
                    "shape endpoint displacement plus independently "
                    "ordered stop endpoints"
                ),
                operational_direction_verified=False,
            )
        )
        candidates.append(
            dict(
                feed_version=version,
                route_id=route,
                direction_id=direction,
                direction_label=label,
                shape_id=shape_id,
                points=points,
                origin=origin,
                length_m=points[-1]["s_m"],
            )
        )
    candidates.sort(key=lambda r: (r["direction_id"], -r["length_m"], r["shape_id"]))
    references, validations, shape_to_ref = [], [], {}
    for candidate in candidates:
        assigned = None
        for reference in references:
            if reference["direction_id"] != candidate["direction_id"]:
                continue
            check = _mapping(candidate, reference, cfg)
            patterns = {
                tuple(s["stop_id"] for s in t["stop_times"]): t["stop_times"]
                for t in uses[candidate["direction_id"], candidate["shape_id"]]
            }
            if check["accepted"]:
                paths = [
                    _stop_path(times, stops, reference, cfg)
                    for times in patterns.values()
                ]
                check["stop_order_valid"] = all(path is not None for path in paths)
                check["accepted"] = check["stop_order_valid"]
                if not check["accepted"]:
                    check["reason"] = "stop_sequence_not_monotone_on_reference"
            validations.append(
                dict(
                    shape_id=candidate["shape_id"],
                    reference_id=reference["reference_id"],
                    **check,
                )
            )
            if check["accepted"]:
                assigned = reference
                break
        if assigned is None:
            identity = (
                f"{version}/{route}/{candidate['direction_id']}/{candidate['shape_id']}"
            )
            assigned = dict(
                candidate,
                reference_id="ref:"
                + hashlib.sha256(identity.encode()).hexdigest()[:20],
                canonical_shape_id=candidate["shape_id"],
                shape_ids=[],
                stop_positions=[],
            )
            assigned.pop("shape_id")
            assigned["family_id"] = "family:" + assigned["reference_id"][4:]
            references.append(assigned)
        assigned["shape_ids"].append(candidate["shape_id"])
        shape_to_ref[candidate["direction_id"], candidate["shape_id"]] = assigned
    pattern_cache, trips = {}, []
    for original in bundle["trips"]:
        if (
            original.get("feed_version", version) != version
            or original["route_id"] != route
        ):
            raise ValueError("Scheduled trip crosses feed/route namespace")
        reference = shape_to_ref[original.get("direction_id", ""), original["shape_id"]]
        pattern = tuple(s["stop_id"] for s in original["stop_times"])
        cache_key = reference["reference_id"], pattern
        if cache_key not in pattern_cache:
            pattern_cache[cache_key] = _stop_path(
                original["stop_times"], stops, reference, cfg
            )
        path = pattern_cache[cache_key]
        scheduled = []
        if path is not None:
            for row, location in zip(original["stop_times"], path, strict=True):
                scheduled.append(
                    dict(
                        row,
                        s_m=location["s_m"],
                        lateral_m=location["lateral_m"],
                        ambiguous=location["ambiguous"],
                    )
                )
                position = dict(
                    stop_id=row["stop_id"],
                    stop_code=stops[row["stop_id"]].get("stop_code"),
                    s_m=location["s_m"],
                    lateral_m=location["lateral_m"],
                    ambiguous=location["ambiguous"],
                )
                if position not in reference["stop_positions"]:
                    reference["stop_positions"].append(position)
        pattern_id = (
            "pattern:" + hashlib.sha256(repr(cache_key).encode()).hexdigest()[:20]
        )
        trips.append(
            dict(
                copy.deepcopy(original),
                reference_id=reference["reference_id"],
                family_id=reference["family_id"],
                pattern_id=pattern_id,
                direction_label=reference["direction_label"],
                scheduled=scheduled,
                canonical_valid=path is not None,
            )
        )
    for reference in references:
        reference["stop_positions"].sort(key=lambda r: (r["s_m"], r["stop_id"]))
    return dict(
        references=references,
        trips=trips,
        validations=validations,
        direction_crosswalk=crosswalk,
        feed_version=version,
        route_id=route,
        date=bundle["date"],
        stop_code_crosswalk=copy.deepcopy(bundle.get("stop_code_crosswalk", {})),
        diagnostics=dict(
            reference_count=len(references),
            shape_count=len(candidates),
            canonical_invalid_trips=sum(not t["canonical_valid"] for t in trips),
            shared_s_across_references=False,
            geometry_units="local equirectangular metres",
            origin=origin,
        ),
    )
