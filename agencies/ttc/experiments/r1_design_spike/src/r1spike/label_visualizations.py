# ruff: noqa: E501
"""Dependency-free, deterministic, repository-safe SVG audit figures."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from html import escape
from typing import Any

PALETTE = (
    "#335c81",
    "#d1495b",
    "#2a9d8f",
    "#edae49",
    "#6d597a",
    "#577590",
    "#8ab17d",
    "#c8553d",
)
INK = "#253238"
GRID = "#d9e1e5"
BACKGROUND = "#ffffff"


def generate_figures(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Return all required figures using aggregate or pseudonymous data only."""

    return {
        "figures/label_distribution_by_task_routing.svg": _routing_matrix(rows),
        "figures/confidence_vs_rationale.svg": _confidence_rationale(rows),
        "figures/chunk_duration_vs_case_grain.svg": _duration_grain(rows),
        "figures/fragment_vs_scheduled_label.svg": _fragment_label(rows),
        "figures/overlap_advantage_vs_confidence.svg": _advantage_scatter(
            rows,
            field="selected_overlap_advantage_s",
            title="Displayed overlap advantage vs confidence",
            x_label="selected minus best other overlap (seconds)",
        ),
        "figures/coverage_advantage_vs_confidence.svg": _advantage_scatter(
            rows,
            field="selected_coverage_advantage_pct_points",
            title="Displayed stop-coverage advantage vs confidence",
            x_label="selected minus best other coverage (percentage points)",
        ),
        "figures/case_readiness_funnel.svg": _readiness_funnel(rows),
        "figures/audit_reason_heatmap.svg": _reason_heatmap(rows),
        "figures/task_routing_flow.svg": _routing_flow(rows),
    }


def _routing_matrix(rows: Sequence[Mapping[str, Any]]) -> str:
    labels = sorted({str(row["original_label"]) for row in rows})
    routes = sorted({str(row["task_routing_suggestion"]) for row in rows})
    counts = Counter(
        (str(row["original_label"]), str(row["task_routing_suggestion"]))
        for row in rows
    )
    return _matrix_svg(
        "First-pass label distribution by task-routing suggestion",
        labels,
        routes,
        counts,
        x_title="recommended review task",
        y_title="preserved first-pass label",
    )


def _confidence_rationale(rows: Sequence[Mapping[str, Any]]) -> str:
    confidences = [1, 2, 3, 4, 5]
    yes = Counter(int(row["confidence"]) for row in rows if row["rationale_present"])
    no = Counter(int(row["confidence"]) for row in rows if not row["rationale_present"])
    width, height = 760, 460
    left, top, plot_w, plot_h = 78, 66, 620, 300
    max_count = max(1, max(yes[c] + no[c] for c in confidences))
    pieces = _svg_open(width, height, "Confidence versus rationale completeness")
    pieces += _axes(left, top, plot_w, plot_h, "confidence", "case count")
    bar_w = 70
    for index, confidence in enumerate(confidences):
        x = left + 48 + index * 112
        base = top + plot_h
        no_h = plot_h * no[confidence] / max_count
        yes_h = plot_h * yes[confidence] / max_count
        pieces.append(_rect(x, base - no_h, bar_w, no_h, PALETTE[1]))
        pieces.append(_rect(x, base - no_h - yes_h, bar_w, yes_h, PALETTE[2]))
        pieces.append(_text(x + bar_w / 2, base + 23, str(confidence), anchor="middle"))
        pieces.append(
            _text(
                x + bar_w / 2,
                base - no_h - yes_h - 7,
                str(no[confidence] + yes[confidence]),
                anchor="middle",
                size=12,
            )
        )
    pieces += _legend(
        [("rationale present", PALETTE[2]), ("rationale absent", PALETTE[1])],
        left,
        420,
    )
    pieces.append("</svg>")
    return "".join(pieces)


def _duration_grain(rows: Sequence[Mapping[str, Any]]) -> str:
    grains = sorted({str(row["case_grain_suggestion"]) for row in rows})
    width, height = 900, 520
    left, top, plot_w = 210, 60, 630
    row_h = 55
    durations = [int(row["chunk_duration_s"]) for row in rows]
    max_duration = max(durations) if durations else 1
    pieces = _svg_open(width, height, "Chunk duration versus case-grain suggestion")
    pieces.append(_line(left, top, left, top + row_h * len(grains), GRID))
    for index, grain in enumerate(grains):
        y = top + index * row_h + row_h / 2
        pieces.append(_line(left, y, left + plot_w, y, GRID))
        pieces.append(_text(left - 12, y + 4, _display(grain), anchor="end", size=12))
    for row in sorted(rows, key=lambda item: str(item["case_id"])):
        grain_index = grains.index(str(row["case_grain_suggestion"]))
        x = left + plot_w * int(row["chunk_duration_s"]) / max_duration
        y = top + grain_index * row_h + row_h / 2
        pieces.append(_circle(x, y, 6, PALETTE[grain_index % len(PALETTE)]))
        pieces.append(_text(x + 9, y - 7, str(row["case_id"]), size=9))
    pieces.append(
        _text(
            left + plot_w / 2,
            top + row_h * len(grains) + 40,
            "chunk duration (seconds)",
            anchor="middle",
        )
    )
    pieces.append("</svg>")
    return "".join(pieces)


def _fragment_label(rows: Sequence[Mapping[str, Any]]) -> str:
    labels = sorted({str(row["original_label"]) for row in rows})
    groups = ["audit-suggested fragment-like", "no fragment audit flag"]
    counts: Counter[tuple[str, str]] = Counter()
    for row in rows:
        group = (
            "audit-suggested fragment-like"
            if "LOCAL_FRAGMENT_NOT_FULL_TRIP" in row["audit_reason_codes"]
            else "no fragment audit flag"
        )
        counts[(group, str(row["original_label"]))] += 1
    return _matrix_svg(
        "Audit-suggested fragment-like status versus scheduled-match label",
        groups,
        labels,
        counts,
        x_title="preserved first-pass label",
        y_title="nonbinding fragment audit grouping",
    )


def _advantage_scatter(
    rows: Sequence[Mapping[str, Any]],
    *,
    field: str,
    title: str,
    x_label: str,
) -> str:
    points = [
        row
        for row in rows
        if row.get(field) is not None and row.get("confidence") is not None
    ]
    width, height = 820, 490
    left, top, plot_w, plot_h = 90, 62, 650, 330
    values = [float(row[field]) for row in points] or [0.0]
    low, high = min(values + [0.0]), max(values + [0.0])
    if math.isclose(low, high):
        low -= 1
        high += 1
    pad = max(1.0, (high - low) * 0.08)
    low -= pad
    high += pad
    pieces = _svg_open(width, height, title)
    pieces += _axes(left, top, plot_w, plot_h, x_label, "confidence")
    zero_x = left + (0 - low) / (high - low) * plot_w
    pieces.append(_line(zero_x, top, zero_x, top + plot_h, "#9aa7ad", dash="4 4"))
    for confidence in range(1, 6):
        y = top + plot_h - (confidence - 1) / 4 * plot_h
        pieces.append(_line(left, y, left + plot_w, y, GRID))
        pieces.append(_text(left - 12, y + 4, str(confidence), anchor="end", size=11))
    for row in sorted(points, key=lambda item: str(item["case_id"])):
        x = left + (float(row[field]) - low) / (high - low) * plot_w
        y = top + plot_h - (int(row["confidence"]) - 1) / 4 * plot_h
        pieces.append(_circle(x, y, 6, PALETTE[0]))
        pieces.append(_text(x + 8, y - 7, str(row["case_id"]), size=9))
    pieces.append(_text(left, top + plot_h + 22, f"{low:.1f}", size=10))
    pieces.append(
        _text(left + plot_w, top + plot_h + 22, f"{high:.1f}", anchor="end", size=10)
    )
    pieces.append("</svg>")
    return "".join(pieces)


def _readiness_funnel(rows: Sequence[Mapping[str, Any]]) -> str:
    total = len(rows)
    consistent_rows = [
        row
        for row in rows
        if row["internal_consistency_status"] == "no_internal_conflict_detected"
    ]
    grain_rows = [
        row
        for row in consistent_rows
        if row["case_grain_suggestion"] == "single_trip_candidate"
        and row["reviewability_suggestion"] == "reviewable_for_schedule_match"
    ]
    key_rows = [
        row
        for row in grain_rows
        if str(row["candidate_key_status"]).startswith("resolved_complete_key")
    ]
    rationale_rows = [row for row in key_rows if row["rationale_present"]]
    second_required = sum(
        "blind_second_pass_required" in row["label_readiness_classes"] for row in rows
    )
    second_complete_rows = [
        row for row in rationale_rows if row.get("blind_second_pass_completed")
    ]
    calibration = sum(bool(row.get("calibration_ready")) for row in rows)
    stages = [
        ("first-pass cases", total),
        ("internally consistent", len(consistent_rows)),
        ("suggested schedule grain", len(grain_rows)),
        ("full key available", len(key_rows)),
        ("rationale available", len(rationale_rows)),
        ("blind second pass completed", len(second_complete_rows)),
        ("calibration-ready", calibration),
    ]
    width, height = 850, 560
    left, top, max_w = 260, 60, 500
    pieces = _svg_open(width, height, "Case-readiness funnel and open gate")
    for index, (label, count) in enumerate(stages):
        y = top + index * 58
        bar_w = max_w * count / max(1, total)
        color = PALETTE[0]
        pieces.append(_text(left - 14, y + 20, label, anchor="end", size=12))
        pieces.append(_rect(left, y, bar_w, 30, color, radius=3))
        pieces.append(_text(left + bar_w + 9, y + 20, f"{count}/{total}", size=12))
    pieces.append(_rect(580, 460, 215, 50, PALETTE[1], opacity=0.14, radius=5))
    pieces.append(_text(592, 480, "open-work branch", size=11, weight="bold"))
    pieces.append(
        _text(
            592, 500, f"blind second pass required: {second_required}/{total}", size=10
        )
    )
    pieces.append(
        _text(
            left,
            height - 22,
            "Bars are cumulative gate stages; the red box is not a funnel stage.",
            size=11,
        )
    )
    pieces.append("</svg>")
    return "".join(pieces)


def _reason_heatmap(rows: Sequence[Mapping[str, Any]]) -> str:
    excluded = {"MODEL_INFORMED_CANDIDATE_SHORTLIST", "PACKET_METADATA_NON_OPERATIONAL"}
    reasons = sorted(
        {
            reason
            for row in rows
            for reason in row["audit_reason_codes"]
            if reason not in excluded
        }
    )
    ordered_rows = sorted(
        rows, key=lambda row: (-int(row["review_priority_score"]), str(row["case_id"]))
    )
    cell = 22
    left, top = 155, 285
    width = left + cell * len(reasons) + 35
    height = top + cell * len(ordered_rows) + 45
    pieces = _svg_open(width, height, "Audit-reason heatmap by case")
    for col, reason in enumerate(reasons):
        x = left + col * cell + 13
        pieces.append(
            f'<text x="{x:.1f}" y="{top - 8}" font-family="sans-serif" '
            f'font-size="9" fill="{INK}" transform="rotate(-58 {x:.1f} {top - 8})">'
            f"{escape(_display(reason))}</text>"
        )
    for row_index, row in enumerate(ordered_rows):
        y = top + row_index * cell
        pieces.append(
            _text(left - 10, y + 15, str(row["case_id"]), anchor="end", size=10)
        )
        present = set(row["audit_reason_codes"])
        for col, reason in enumerate(reasons):
            pieces.append(
                _rect(
                    left + col * cell,
                    y,
                    cell - 2,
                    cell - 2,
                    PALETTE[1] if reason in present else "#eef2f3",
                )
            )
    pieces.append("</svg>")
    return "".join(pieces)


def _routing_flow(rows: Sequence[Mapping[str, Any]]) -> str:
    left_nodes = sorted({str(row["original_label"]) for row in rows})
    right_nodes = sorted({str(row["task_routing_suggestion"]) for row in rows})
    flows = Counter(
        (str(row["original_label"]), str(row["task_routing_suggestion"]))
        for row in rows
    )
    width, height = 980, 550
    left_x, right_x = 230, 730
    top, gap = 70, 95
    left_y = {node: top + index * gap for index, node in enumerate(left_nodes)}
    right_gap = (height - 130) / max(1, len(right_nodes) - 1)
    right_y = {node: top + index * right_gap for index, node in enumerate(right_nodes)}
    pieces = _svg_open(
        width, height, "Task-routing flow from preserved label to review task"
    )
    pieces.append(_text(left_x, 38, "preserved label", anchor="middle", weight="bold"))
    pieces.append(
        _text(right_x, 38, "suggested review task", anchor="middle", weight="bold")
    )
    for (source, target), count in sorted(flows.items()):
        pieces.append(
            f'<path d="M {left_x + 90} {left_y[source]} C {left_x + 260} {left_y[source]}, '
            f'{right_x - 260} {right_y[target]}, {right_x - 130} {right_y[target]}" '
            f'stroke="{PALETTE[left_nodes.index(source) % len(PALETTE)]}" '
            f'stroke-width="{1 + count * 2}" opacity="0.55" fill="none"/>'
        )
    for node, y in left_y.items():
        pieces.append(_rect(left_x - 110, y - 17, 200, 34, "#e8eef2", radius=5))
        pieces.append(
            _text(left_x - 10, y + 5, _display(node), anchor="middle", size=11)
        )
    for node, y in right_y.items():
        pieces.append(_rect(right_x - 130, y - 17, 245, 34, "#e8eef2", radius=5))
        pieces.append(
            _text(right_x - 8, y + 5, _display(node), anchor="middle", size=10)
        )
    pieces.append("</svg>")
    return "".join(pieces)


def _matrix_svg(
    title: str,
    rows: Sequence[str],
    columns: Sequence[str],
    counts: Mapping[tuple[str, str], int],
    *,
    x_title: str,
    y_title: str,
) -> str:
    cell_w, cell_h = 150, 52
    left, top = 215, 110
    width = left + cell_w * len(columns) + 30
    height = top + cell_h * len(rows) + 75
    max_count = max([1, *counts.values()])
    pieces = _svg_open(width, height, title)
    for index, column in enumerate(columns):
        pieces.append(
            _text(
                left + index * cell_w + cell_w / 2,
                top - 20,
                _display(column),
                anchor="middle",
                size=10,
            )
        )
    for row_index, row in enumerate(rows):
        y = top + row_index * cell_h
        pieces.append(_text(left - 12, y + 32, _display(row), anchor="end", size=11))
        for col_index, column in enumerate(columns):
            count = counts.get((row, column), 0)
            opacity = 0.12 + 0.78 * count / max_count
            pieces.append(
                _rect(
                    left + col_index * cell_w,
                    y,
                    cell_w - 3,
                    cell_h - 3,
                    PALETTE[0],
                    opacity=opacity,
                )
            )
            pieces.append(
                _text(
                    left + col_index * cell_w + cell_w / 2,
                    y + 31,
                    str(count),
                    anchor="middle",
                    size=13,
                    weight="bold",
                )
            )
    pieces.append(
        _text(
            left + cell_w * len(columns) / 2,
            height - 22,
            x_title,
            anchor="middle",
            size=11,
        )
    )
    pieces.append(
        f'<text x="25" y="{top + cell_h * len(rows) / 2:.1f}" '
        f'font-family="sans-serif" font-size="11" fill="{INK}" '
        f'transform="rotate(-90 25 {top + cell_h * len(rows) / 2:.1f})">'
        f"{escape(y_title)}</text>"
    )
    pieces.append("</svg>")
    return "".join(pieces)


def _svg_open(width: int, height: int, title: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img">',
        f"<title>{escape(title)}</title>",
        _rect(0, 0, width, height, BACKGROUND),
        _text(width / 2, 30, title, anchor="middle", size=17, weight="bold"),
    ]


def _axes(
    left: float,
    top: float,
    width: float,
    height: float,
    x_label: str,
    y_label: str,
) -> list[str]:
    return [
        _line(left, top, left, top + height, INK),
        _line(left, top + height, left + width, top + height, INK),
        _text(left + width / 2, top + height + 48, x_label, anchor="middle", size=11),
        f'<text x="24" y="{top + height / 2:.1f}" font-family="sans-serif" '
        f'font-size="11" fill="{INK}" transform="rotate(-90 24 {top + height / 2:.1f})">'
        f"{escape(y_label)}</text>",
    ]


def _legend(items: Sequence[tuple[str, str]], x: float, y: float) -> list[str]:
    pieces: list[str] = []
    offset = 0.0
    for label, color in items:
        pieces.append(_rect(x + offset, y - 12, 15, 15, color))
        pieces.append(_text(x + offset + 21, y, label, size=11))
        offset += 155
    return pieces


def _rect(
    x: float,
    y: float,
    width: float,
    height: float,
    fill: str,
    *,
    opacity: float = 1.0,
    radius: float = 0,
) -> str:
    return (
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}" '
        f'fill="{fill}" opacity="{opacity:.3f}" rx="{radius:.1f}"/>'
    )


def _line(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    stroke: str,
    *,
    dash: str | None = None,
) -> str:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="{stroke}" stroke-width="1"{dash_attr}/>'
    )


def _circle(x: float, y: float, radius: float, fill: str) -> str:
    return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" fill="{fill}"/>'


def _text(
    x: float,
    y: float,
    value: str,
    *,
    anchor: str = "start",
    size: int = 12,
    weight: str = "normal",
) -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="sans-serif" font-size="{size}" '
        f'font-weight="{weight}" text-anchor="{anchor}" fill="{INK}">'
        f"{escape(value)}</text>"
    )


def _display(value: str) -> str:
    return value.replace("_", " ")
