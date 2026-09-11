from datetime import date

from labeling import adapters
from labeling.adapters import ArtifactStore, discover, load_cases


def test_discover_bare_csv(tmp_path):
    csv = tmp_path / "cases.csv"
    csv.write_text("case_id,summary\nC-1,\"chunk of 10s\"\n")
    store = discover(csv, None)
    assert store.cases_csv == csv.resolve()
    assert store.run_dir is None
    # no artifacts root found -> everything optional is None, with a note
    assert store.normalized_dir is None
    assert store.gtfs_root is None
    assert store.notes


def test_discover_full_layout(tmp_path):
    root = tmp_path / "artifacts"
    run = root / "experimental-x-run"
    run.mkdir(parents=True)
    (run / "labeling_packet_redacted.csv").write_text("case_id\nC-1\n")
    (root / "diagnostic-2026-01-01" / "normalized").mkdir(parents=True)
    (root / "gtfs-2023-11-28-feedx").mkdir()
    store = discover(run / "labeling_packet_redacted.csv", root)
    assert store.run_dir == run
    assert store.normalized_dir == root / "diagnostic-2026-01-01" / "normalized"
    assert store.gtfs_root == root


def test_load_cases_fills_missing_columns(tmp_path):
    csv = tmp_path / "cases.csv"
    csv.write_text("case_id\nC-1\nC-2\n")
    store = discover(csv, None)
    df = load_cases(store)
    assert df.height == 2
    for col in ["stratum", "kind", "n_events", "span_rel_s", "summary", "case_order"]:
        assert col in df.columns
    assert df["case_order"].to_list() == [0, 1]


def test_load_cases_unreadable(tmp_path):
    store = discover(tmp_path / "missing.csv", None)
    assert load_cases(store) is None


def test_missing_artifacts_return_none(tmp_path):
    store = ArtifactStore(cases_csv=tmp_path / "cases.csv")
    assert adapters.load_restricted_packet(store) is None
    assert adapters.load_refined_chunks(store) is None
    assert adapters.load_avl_day(store, date(2023, 11, 28)) is None
    assert adapters.load_apc_day(store, date(2023, 11, 28)) is None
    assert adapters.gtfs_dir_for(store, date(2023, 11, 28), "feed") is None
    assert adapters.load_scheduled_trips(None) is None
    assert adapters.load_shape(None, "s") is None
    assert adapters.load_stop_events_for_trip(None, None, "t") is None


def test_gtfs_dir_for(tmp_path):
    root = tmp_path / "artifacts"
    (root / "gtfs-2023-11-28-feedx").mkdir(parents=True)
    csv = tmp_path / "cases.csv"
    csv.write_text("case_id\nC-1\n")
    store = ArtifactStore(cases_csv=csv, gtfs_root=root)
    assert adapters.gtfs_dir_for(store, date(2023, 11, 28), "feedx") is not None
    assert adapters.gtfs_dir_for(store, date(2023, 11, 29), "feedx") is None
    assert adapters.gtfs_dir_for(store, None, "feedx") is None
