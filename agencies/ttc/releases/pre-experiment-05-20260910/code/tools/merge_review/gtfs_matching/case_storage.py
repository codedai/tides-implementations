"""Versioned transport encoding; matching results retain their legacy API shape.

Only the duplicate top-level global result is elided. No observations, candidate
scores, windows or partial-mode evidence are removed. Historical unmarked JSON
continues to decode as before. Hashes must always cover the original stored bytes,
not a reconstructed logical object.
"""

from __future__ import annotations

import copy
import json

STORAGE_KEY = "_case_storage"
STORAGE_DESCRIPTOR = {
    "schema": "gtfs-case-single-global-v1",
    "global_result_path": "matching_modes.global.result",
}


def _global_result(artifact):
    modes = artifact.get("matching_modes")
    if not isinstance(modes, dict):
        raise ValueError("Single-global storage requires matching_modes")
    mode = modes.get("global")
    if not isinstance(mode, dict) or not isinstance(mode.get("result"), dict):
        raise ValueError("Single-global storage requires a global result object")
    return mode["result"]


def encode_case_artifact(artifact):
    """Return a storage view without modifying the supplied logical artifact.

    Global-only historical shapes remain unmarked. Two global entries must be
    exactly equal in their JSON representation before one can be omitted: Python
    equality alone conflates booleans, integers, floats and signed zero. Preserve
    nested key order too, because replay signatures can depend on it.
    """
    if not isinstance(artifact, dict):
        raise ValueError("A case artifact must be an object")
    if STORAGE_KEY in artifact:
        raise ValueError("Decode a stored case before writing its logical artifact")
    modes = artifact.get("matching_modes")
    if not isinstance(modes, dict) or "global" not in modes:
        return dict(artifact)
    canonical = _global_result(artifact)
    original = artifact.get("result")
    if not isinstance(original, dict):
        raise ValueError("Logical artifact requires its top-level global result")
    if original is not canonical and json.dumps(
        original, separators=(",", ":"), allow_nan=False
    ) != json.dumps(canonical, separators=(",", ":"), allow_nan=False):
        raise ValueError("Global result copies differ; refusing lossy deduplication")
    stored = {key: value for key, value in artifact.items() if key != "result"}
    stored[STORAGE_KEY] = dict(STORAGE_DESCRIPTOR)
    return stored


def decode_case_artifact(stored):
    """Expose both legacy result access paths for legacy and compact payloads.

    Generic unmarked metadata/arrays are passed through for shared JSON readers.
    Marked payloads fail closed if malformed; the reference target is fixed, never
    an arbitrary JSON pointer, filename or URL. Reconstruct an independent copy
    for the legacy entry, preserving the mutation isolation of old JSON loads.
    """
    if not isinstance(stored, dict) or STORAGE_KEY not in stored:
        return stored
    descriptor = stored[STORAGE_KEY]
    if not isinstance(descriptor, dict) or descriptor != STORAGE_DESCRIPTOR:
        raise ValueError("Unknown or malformed case storage descriptor")
    if "result" in stored:
        raise ValueError("Single-global storage must not contain a top-level result")
    canonical = _global_result(stored)
    logical = {key: value for key, value in stored.items() if key != STORAGE_KEY}
    logical["result"] = copy.deepcopy(canonical)
    return logical
