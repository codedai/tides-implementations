"""Offline command-line interface for the GTFS preprocessing stage.

Commands (all offline; nothing downloads anything):

- ``ttc-tides gtfs inventory``  — hash and list local GTFS archives;
- ``ttc-tides gtfs manifest``   — build a draft feed manifest for review;
- ``ttc-tides gtfs select``     — resolve which feed serves given date(s);
- ``ttc-tides gtfs process``    — normalize a feed and derive its schedule
  artifacts for a service date;
- ``ttc-tides gtfs evaluate``   — run the preprocessing quality evaluation
  and write a machine-readable report;
- ``ttc-tides diagnostic run``  — provisional S0–S2 AVL/APC landing and
  normalization (non-publishable; authorized by ADR 0002).

Exit codes: 0 success, 1 fatal data/structural error, 2 usage error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

import polars as pl

from .ingest.gtfs import (
    FatalGtfsError,
    discover_archives,
    inventory_archive,
)
from .ingest.sources import FatalSourceError
from .normalize.gtfs import normalize_archive
from .pipeline import main_summary, run_diagnostic
from .quality.gtfs import evaluate_feed
from .reconcile.feeds import (
    build_manifest_draft,
    load_manifest,
    manifest_service_coverage,
    resolve_manifest_archive,
    select_feed,
    write_manifest,
)
from .reconcile.schedule import (
    DEFAULT_TIMEZONE,
    derive_patterns,
    feed_daily_services,
    scheduled_stop_events_for_date,
    scheduled_trips_for_date,
    service_day_anchor,
)

DEFAULT_GTFS_DIR = Path("database_offline/gtfs")
DEFAULT_MANIFEST = Path("config/feed_manifest.csv")
DEFAULT_ARTIFACTS_DIR = Path("artifacts")


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def _parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an ISO date (YYYY-MM-DD)"
        ) from exc


def _ensure_output_outside_source(path: Path, gtfs_dir: Path) -> None:
    """Protect the immutable GTFS input directory from generated artifacts."""
    output = path.resolve()
    source = gtfs_dir.resolve()
    try:
        output.relative_to(source)
    except ValueError:
        return
    raise FatalGtfsError(
        f"refusing output inside immutable GTFS source-data boundary {source}: {output}"
    )


def _prepare_output(path: Path, force: bool, *, directory: bool = False) -> None:
    if path.exists():
        if not force:
            raise FatalGtfsError(
                f"refusing to overwrite existing output {path}; pass --force"
            )
        if path.is_dir() and not path.is_symlink():
            if not directory:
                raise FatalGtfsError(
                    f"refusing to replace directory with file output {path}"
                )
            shutil.rmtree(path)
        else:
            path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)


def _resolve_archive_path(gtfs_dir: Path, feed: str) -> Path:
    candidate = Path(feed)
    if candidate.is_file():
        return candidate
    matches = [p for p in discover_archives(gtfs_dir) if feed in p.name]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FatalGtfsError(
            f"no archive in {gtfs_dir} matches {feed!r} "
            "(pass a path or a filename fragment)"
        )
    raise FatalGtfsError(
        f"{feed!r} is ambiguous in {gtfs_dir}: {[p.name for p in matches]}"
    )


# Commands ----------------------------------------------------------------------


def cmd_inventory(args: argparse.Namespace) -> int:
    rows = []
    for path in discover_archives(args.gtfs_dir):
        archive = inventory_archive(
            path,
            compute_content_hash=not args.fast,
            feed_id=f"file-{path.stem[:16]}" if args.fast else None,
        )
        member_min, member_max = archive.member_date_range()
        rows.append(
            {
                "archive": path.name,
                "feed_id": archive.feed_id if archive.content_hash else "(skipped)",
                "sha256": archive.archive_sha256[:12],
                "content_hash": (
                    archive.content_hash[:12] if archive.content_hash else ""
                ),
                "members": len(archive.members),
                "missing_required": ",".join(archive.missing_required_members()),
                "member_dates": f"{member_min:%Y-%m-%d}..{member_max:%Y-%m-%d}",
            }
        )
    with pl.Config(tbl_rows=-1, fmt_str_lengths=60, tbl_hide_dataframe_shape=True):
        print(pl.DataFrame(rows))
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    _ensure_output_outside_source(args.out, args.gtfs_dir)
    draft = build_manifest_draft(args.gtfs_dir)
    _prepare_output(args.out, args.force)
    write_manifest(draft, args.out)
    with pl.Config(tbl_rows=-1, fmt_str_lengths=40, tbl_hide_dataframe_shape=True):
        print(
            draft.select(
                "priority",
                "feed_id",
                "relative_path",
                "calendar_min",
                "calendar_max",
                "status",
            )
        )
    print(f"\ndraft manifest written to {args.out}")
    print(
        "review provenance and priorities, then promote rows to "
        f"status=approved in {DEFAULT_MANIFEST}"
    )
    return 0


def _load_selection_inputs(
    args: argparse.Namespace,
    service_dates: tuple[dt.date, ...],
) -> tuple[pl.DataFrame, pl.DataFrame]:
    manifest = load_manifest(args.manifest)
    coverage = manifest_service_coverage(
        manifest,
        args.gtfs_dir,
        service_dates=service_dates,
        allow_candidate=args.allow_candidate,
    )
    return manifest, coverage


def cmd_select(args: argparse.Namespace) -> int:
    manifest, coverage = _load_selection_inputs(args, tuple(args.dates))
    rows = []
    for service_date in args.dates:
        selection = select_feed(
            manifest,
            coverage,
            service_date,
            allow_candidate=args.allow_candidate,
        )
        rows.append(
            {
                "service_date": str(selection.service_date),
                "feed_id": selection.feed_id or "(none)",
                "reason": selection.reason,
                "candidates": ",".join(selection.candidate_feed_ids),
            }
        )
    with pl.Config(tbl_rows=-1, fmt_str_lengths=80, tbl_hide_dataframe_shape=True):
        print(pl.DataFrame(rows))
    return 0


def cmd_process(args: argparse.Namespace) -> int:
    if args.out is not None:
        _ensure_output_outside_source(args.out, args.gtfs_dir)
    # Validate configuration before selecting/normalizing data or creating an
    # output boundary. The computed value is reused conceptually downstream.
    service_day_anchor(args.date, args.timezone)
    manifest, coverage = _load_selection_inputs(args, (args.date,))
    selection = select_feed(
        manifest,
        coverage,
        args.date,
        allow_candidate=args.allow_candidate,
    )
    if not selection.selected:
        return _fail(f"no feed with active service on {args.date}; nothing to process")
    row = manifest.filter(pl.col("feed_id") == selection.feed_id).row(0, named=True)
    archive_path = resolve_manifest_archive(args.gtfs_dir, row["relative_path"])
    archive = inventory_archive(
        archive_path,
        compute_content_hash=False,
        feed_id=row["feed_id"],
        known_content_hash=row["content_hash"],
    )
    if archive.archive_sha256 != row["archive_sha256"]:
        raise FatalGtfsError(
            f"{archive_path} SHA-256 does not match the manifest pin for "
            f"{row['feed_id']}; the input changed during processing"
        )

    out_dir = args.out or (
        DEFAULT_ARTIFACTS_DIR / f"gtfs-{args.date}-{selection.feed_id}"
    )
    _ensure_output_outside_source(out_dir, args.gtfs_dir)
    _prepare_output(out_dir, args.force, directory=True)

    print(f"processing {archive.source_file} as {selection.feed_id} for {args.date}")
    feed = normalize_archive(archive, ingest_run_id=out_dir.name)

    report = evaluate_feed(feed)
    report_dict = report.to_dict()
    report_dict["selection"] = {
        "service_date": str(args.date),
        "feed_id": selection.feed_id,
        "reason": selection.reason,
        "candidates": list(selection.candidate_feed_ids),
    }

    normalized_dir = out_dir / "normalized" / "gtfs"
    quality_dir = out_dir / "quality"
    for directory in (normalized_dir, quality_dir):
        directory.mkdir(parents=True, exist_ok=True)

    for name, member in feed.members.items():
        stem = name.removesuffix(".txt")
        member.normalized.write_parquet(normalized_dir / f"{stem}.parquet")
        if member.quarantined.height:
            member.quarantined.write_parquet(quality_dir / f"quarantine_{stem}.parquet")

    report_path = quality_dir / "gtfs_quality_report.json"
    report_path.write_text(
        json.dumps(report_dict, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    if report.fatal_findings:
        return _fail(
            f"fatal quality findings: {report.fatal_findings}; "
            f"diagnostics written to {out_dir}"
        )

    services = feed_daily_services(feed)
    trips = scheduled_trips_for_date(feed, args.date, daily_services=services)
    events = scheduled_stop_events_for_date(
        feed, args.date, timezone=args.timezone, daily_services=services
    )
    patterns, trip_patterns = derive_patterns(feed)
    matched_dir = out_dir / "matched" / "gtfs"
    matched_dir.mkdir(parents=True, exist_ok=True)
    services.write_parquet(matched_dir / "daily_services.parquet")
    trips.write_parquet(matched_dir / "scheduled_trips.parquet")
    events.write_parquet(matched_dir / "scheduled_stop_events.parquet")
    patterns.write_parquet(matched_dir / "patterns.parquet")
    trip_patterns.write_parquet(matched_dir / "trip_patterns.parquet")

    print(
        f"scheduled trips: {trips.height}; stop events: {events.height}; "
        f"patterns: {patterns.height}; quarantined rows: {report.quarantined_rows}"
    )
    print(f"artifacts written to {out_dir}")
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    if args.all:
        paths = discover_archives(args.gtfs_dir)
    elif args.feed:
        paths = [_resolve_archive_path(args.gtfs_dir, feed) for feed in args.feed]
    else:
        return _fail("pass --feed <path-or-fragment> (repeatable) or --all")

    exit_code = 0
    summaries = []
    _ensure_output_outside_source(args.out, args.gtfs_dir)
    _prepare_output(args.out, args.force, directory=True)
    args.out.mkdir(parents=True, exist_ok=True)
    for path in paths:
        archive = None
        try:
            archive = inventory_archive(path)
            print(f"evaluating {path.name} ({archive.feed_id}) ...")
            feed = normalize_archive(archive, ingest_run_id="evaluate")
        except FatalGtfsError as exc:
            print(f"  FATAL: {exc}")
            summaries.append(
                {
                    "archive": path.name,
                    "feed_id": archive.feed_id if archive else "(unavailable)",
                    "fatal": 1,
                    "warnings": 0,
                    "quarantined_rows": 0,
                }
            )
            exit_code = 1
            continue
        report = evaluate_feed(feed)
        report_path = args.out / f"{archive.feed_id}.json"
        report_path.write_text(
            json.dumps(report.to_dict(), indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        for finding in report.findings:
            print(
                f"  [{finding.severity}] {finding.code} x{finding.count} "
                f"({finding.member}): {finding.description}"
            )
        summaries.append(
            {
                "archive": path.name,
                "feed_id": archive.feed_id,
                "fatal": len(report.fatal_findings),
                "warnings": len(
                    [f for f in report.findings if f.severity == "warning"]
                ),
                "quarantined_rows": report.quarantined_rows,
            }
        )
        if report.fatal_findings:
            exit_code = 1

    with pl.Config(tbl_rows=-1, fmt_str_lengths=60, tbl_hide_dataframe_shape=True):
        print(pl.DataFrame(summaries))
    print(f"reports written to {args.out}")
    return exit_code


def cmd_diagnostic_run(args: argparse.Namespace) -> int:
    if args.avl is None and args.apc is None:
        return _fail("pass --avl and/or --apc source file(s)")
    result = run_diagnostic(
        avl_path=args.avl,
        apc_path=args.apc,
        out_dir=args.out,
        force=args.force,
        ingest_run_id=args.run_id,
    )
    print(main_summary(result))
    return 0


# Parser ------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ttc-tides",
        description="Offline TTC-to-TIDES prototype CLI (GTFS stage)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    gtfs = subparsers.add_parser("gtfs", help="GTFS preprocessing commands")
    gtfs_sub = gtfs.add_subparsers(dest="gtfs_command", required=True)

    def add_gtfs_dir(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--gtfs-dir",
            type=Path,
            default=DEFAULT_GTFS_DIR,
            help=f"directory of GTFS ZIP archives (default: {DEFAULT_GTFS_DIR})",
        )

    def add_candidate_opt_in(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--allow-candidate",
            action="store_true",
            help=(
                "allow unapproved status=candidate rows from a draft manifest "
                "(prototype investigation only)"
            ),
        )

    p_inventory = gtfs_sub.add_parser(
        "inventory", help="hash and list local GTFS archives"
    )
    add_gtfs_dir(p_inventory)
    p_inventory.add_argument(
        "--fast",
        action="store_true",
        help="skip content hashing (no feed_id; file SHA-256 only)",
    )
    p_inventory.set_defaults(func=cmd_inventory)

    p_manifest = gtfs_sub.add_parser(
        "manifest", help="build a draft feed manifest for human review"
    )
    add_gtfs_dir(p_manifest)
    p_manifest.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_ARTIFACTS_DIR / "feed_manifest.draft.csv",
        help="where to write the draft manifest CSV",
    )
    p_manifest.add_argument("--force", action="store_true")
    p_manifest.set_defaults(func=cmd_manifest)

    p_select = gtfs_sub.add_parser(
        "select", help="resolve the feed serving given service date(s)"
    )
    add_gtfs_dir(p_select)
    p_select.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"reviewed feed manifest (default: {DEFAULT_MANIFEST})",
    )
    p_select.add_argument(
        "--date",
        dest="dates",
        type=_parse_date,
        action="append",
        required=True,
        help="service date, repeatable (YYYY-MM-DD)",
    )
    add_candidate_opt_in(p_select)
    p_select.set_defaults(func=cmd_select)

    p_process = gtfs_sub.add_parser(
        "process",
        help="normalize the selected feed and derive schedule artifacts",
    )
    add_gtfs_dir(p_process)
    p_process.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"reviewed feed manifest (default: {DEFAULT_MANIFEST})",
    )
    p_process.add_argument("--date", type=_parse_date, required=True)
    p_process.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"agency timezone (default: {DEFAULT_TIMEZONE})",
    )
    p_process.add_argument(
        "--out",
        type=Path,
        default=None,
        help="artifact directory (default: artifacts/gtfs-<date>-<feed_id>)",
    )
    add_candidate_opt_in(p_process)
    p_process.add_argument("--force", action="store_true")
    p_process.set_defaults(func=cmd_process)

    p_evaluate = gtfs_sub.add_parser(
        "evaluate", help="run the GTFS preprocessing quality evaluation"
    )
    add_gtfs_dir(p_evaluate)
    evaluate_source = p_evaluate.add_mutually_exclusive_group(required=True)
    evaluate_source.add_argument(
        "--feed",
        action="append",
        help="archive path or filename fragment; repeatable",
    )
    evaluate_source.add_argument(
        "--all", action="store_true", help="evaluate every archive in --gtfs-dir"
    )
    p_evaluate.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_ARTIFACTS_DIR / "gtfs-quality",
        help="directory for JSON quality reports",
    )
    p_evaluate.add_argument("--force", action="store_true")
    p_evaluate.set_defaults(func=cmd_evaluate)

    diagnostic = subparsers.add_parser(
        "diagnostic",
        help="provisional S0-S2 AVL/APC diagnostics (non-publishable, ADR 0002)",
    )
    diagnostic_sub = diagnostic.add_subparsers(dest="diagnostic_command", required=True)
    p_diag_run = diagnostic_sub.add_parser(
        "run",
        help="land and normalize AVL/APC source files with quality reports",
    )
    p_diag_run.add_argument(
        "--avl", type=Path, default=None, help="AVL source CSV file"
    )
    p_diag_run.add_argument(
        "--apc", type=Path, default=None, help="APC source CSV file"
    )
    p_diag_run.add_argument(
        "--out",
        type=Path,
        required=True,
        help="run directory for normalized Parquet, rejections, and reports",
    )
    p_diag_run.add_argument(
        "--run-id", default=None, help="ingest run id (default: output dir name)"
    )
    p_diag_run.add_argument("--force", action="store_true")
    p_diag_run.set_defaults(func=cmd_diagnostic_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FatalGtfsError, FatalSourceError) as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
