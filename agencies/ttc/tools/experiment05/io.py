"""Small JSON metadata and typed, compressed, source-preserving stage tables."""

from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from datetime import date, datetime
from pathlib import Path

import polars as pl


def reject_symlinks(path):
    path = Path(path).absolute()
    if ".." in path.parts:
        raise ValueError("Parent traversal is not a pipeline path")
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError(f"Symlink is not a pipeline output/input artifact: {item}")
    return path


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def json_safe(value):
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Path):
        return str(value)
    return value


def read_json(path):
    return json.loads(reject_symlinks(path).read_text())


def write_json(path, value):
    path = reject_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".writing")
    # Only pipeline-owned metadata is updated. Successful stage payloads remain
    # hash-bound; a failed stage is never a successful cache entry.
    with temporary.open("x") as stream:
        json.dump(
            json_safe(value),
            stream,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def write_rows(path, rows, *, columns=()):
    """Nested diagnostics are JSON columns; union keys never vanish by sampling."""
    path = reject_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = [json_safe(row) for row in rows]
    names = sorted(set(columns) | {key for row in safe for key in row})
    nested = {
        key
        for row in safe
        for key, value in row.items()
        if isinstance(value, (dict, list))
    }
    encoded = [
        {
            key: json.dumps(row.get(key), sort_keys=True, separators=(",", ":"))
            if key in nested and row.get(key) is not None
            else row.get(key)
            for key in names
        }
        for row in safe
    ]
    frame = (
        pl.DataFrame(encoded, infer_schema_length=None)
        if encoded
        else pl.DataFrame(schema={name: pl.String for name in names})
    )
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".writing")
    frame.write_parquet(temporary, compression="zstd")
    os.replace(temporary, path)
    write_json(
        path.with_suffix(".schema.json"),
        {
            "schema": "experiment05-table-v1",
            "json_columns": sorted(nested),
            "columns": {key: str(dtype) for key, dtype in frame.schema.items()},
            "row_count": frame.height,
            "sha256": sha256(path),
        },
    )
    return frame.height


def read_rows(path):
    path = reject_symlinks(path)
    schema = read_json(path.with_suffix(".schema.json"))
    if sha256(path) != schema["sha256"]:
        raise ValueError(f"Stage table hash changed: {path}")
    rows = pl.read_parquet(path).to_dicts()
    if len(rows) != schema["row_count"]:
        raise ValueError("Stage table row-count mismatch")
    for row in rows:
        for key in schema["json_columns"]:
            if row.get(key) is not None:
                row[key] = json.loads(row[key])
    return rows


def concatenate_rows(path, sources, *, columns=()):
    """Stream ordered, verified table shards without decoding nested JSON rows."""
    path = reject_symlinks(path)
    descriptors = []
    nested = set()
    for source in sources:
        source = reject_symlinks(source)
        descriptor = read_json(source.with_suffix(".schema.json"))
        if sha256(source) != descriptor["sha256"]:
            raise ValueError(f"Stage shard hash changed: {source}")
        if descriptor["row_count"]:
            descriptors.append((source, descriptor))
            nested.update(descriptor["json_columns"])
    if not descriptors:
        return write_rows(path, [], columns=columns)
    frames = []
    for source, descriptor in descriptors:
        frame = pl.scan_parquet(source)
        # A diagnostic may be structured only in some shards. Normalize a
        # scalar from another shard to valid JSON before the common union.
        for name in nested - set(descriptor["json_columns"]):
            if name in descriptor["columns"]:
                frame = frame.with_columns(
                    pl.col(name).map_elements(
                        lambda value: json.dumps(value, sort_keys=True),
                        return_dtype=pl.String,
                    )
                )
        frames.append(frame)
    joined = pl.concat(frames, how="diagonal_relaxed")
    schema = joined.collect_schema()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".writing")
    joined.sink_parquet(temporary, compression="zstd", maintain_order=True)
    os.replace(temporary, path)
    count = sum(descriptor["row_count"] for _, descriptor in descriptors)
    write_json(
        path.with_suffix(".schema.json"),
        {
            "schema": "experiment05-table-v1",
            "json_columns": sorted(nested),
            "columns": {key: str(dtype) for key, dtype in schema.items()},
            "row_count": count,
            "sha256": sha256(path),
        },
    )
    return count


def stable_id(namespace, *parts):
    digest = hashlib.sha256(
        json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"{namespace}:{digest[:32]}"
