"""Conservatively merge approved exact-state native practice memories.

The base input has priority. Different evidence for an existing state/move is
held for review, never replaced because of a higher score or search depth.
Inputs and active installation are read-only; fresh runtime checks still apply.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

import gambit_practice_memory as memory

ROOT = Path(__file__).resolve().parents[1]
MERGE_POLICY = "regicide-gambit-memory-merge-v1"


def _sha(blob):
    return hashlib.sha256(blob).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key in memory input")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Nonfinite JSON value in memory input")


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")


def _read_input(path, index):
    path = Path(path).resolve(strict=True)
    blob = path.read_bytes()
    if not 0 < len(blob) <= memory.MAX_BYTES:
        raise ValueError("Memory input exceeds the runtime loader size limit")
    try:
        value = json.loads(blob, object_pairs_hook=_object, parse_constant=_reject_constant)
        memory._validate(value)
        acceptance = value.get("acceptance")
        if value.get("status") != "approved" or not isinstance(acceptance, dict) or acceptance.get("passed") is not True:
            raise ValueError("Only approved inputs with passed acceptance can be merged")
        keys = value.get("heldout_position_keys", [])
        if not isinstance(keys, list) or any(type(key) is not str for key in keys):
            raise ValueError("Heldout position keys must be a list of strings")
        for row in value["records"]:
            if any(type(row.get(key)) is not str or not row[key] for key in ("record_id", "case_id", "family_id")):
                raise ValueError("Memory records require nonempty record/case/family identifiers")
    except (KeyError, TypeError, UnicodeError, OverflowError) as error:
        raise ValueError("Malformed memory input: " + str(path)) from error
    info = {"input_id": f"input-{index:03d}", "role": "base" if index == 0 else "addition",
            "path": str(path), "sha256": _sha(blob), "bytes": len(blob), "memory_id": value["memory_id"],
            "snapshot": f"inputs/{index:03d}-{'base' if index == 0 else 'addition'}.json",
            "source_sha256": copy.deepcopy(value.get("source_sha256")),
            "curriculum_sha256": copy.deepcopy(value.get("curriculum_sha256")),
            "derived_source_inputs_sha256": value.get("derived_source_inputs_sha256"),
            "derived_curriculum_inputs_sha256": value.get("derived_curriculum_inputs_sha256")}
    return {"path": path, "blob": blob, "value": value, "info": info}


def _new_output(path):
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise FileExistsError("Use a new output directory; existing files are preserved")
    path = path.resolve()
    active = memory.DEFAULT_MEMORY.resolve()
    sources = (ROOT / "sources", ROOT.parent / "sources")
    if path == active or path == active.parent or any(path.is_relative_to(source.resolve()) for source in sources):
        raise ValueError("Active memory and synced sources are not merge outputs")
    return path


def _write_new(path, blob):
    """Publish flushed bytes without replacing a competing destination."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".merge-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _ref(info, row, item=None):
    result = {"input_id": info["input_id"], "record_id": row["record_id"], "case_id": row["case_id"]}
    if item is not None:
        result.update(query_id=item["query_id"], evidence_sha256=memory.digest(item))
    return result


def merge(base, inputs, output):
    """Return a loader-compatible merged artifact; never activate it."""
    output = _new_output(output)
    loaded = [_read_input(path, index) for index, path in enumerate([base, *inputs])]
    identity = loaded[0]["value"]["engine_identity"]
    if any(entry["value"]["engine_identity"] != identity for entry in loaded[1:]):
        raise ValueError("All memory inputs must use the same binary and evaluator identity")
    heldout_families = sorted({family for entry in loaded for family in entry["value"]["heldout_family_ids"]})
    heldout_keys = sorted({key for entry in loaded for key in entry["value"].get("heldout_position_keys", [])})
    reserved = set(heldout_keys)
    groups, conflicts, excluded, duplicates, capacity, mappings = {}, [], [], [], [], []
    for entry in loaded:
        info = entry["info"]
        # Runtime lookup prioritizes record identifiers before deduplicating a
        # move, so retain that same base priority rather than score-sort rows.
        for row in sorted(entry["value"]["records"], key=lambda item: item["record_id"]):
            origin = _ref(info, row)
            board = memory._board(row["history_uci"])
            overlap = {memory.position_key(board)} & reserved
            for item in row["candidates"]:
                child = board.copy(stack=True)
                child.push_uci(item["uci"])
                overlap |= {memory.position_key(child)} & reserved
            reason = ("heldout_family_union" if row["family_id"] in heldout_families else
                      "heldout_root_or_child_union" if overlap else
                      "inactive_runtime_context" if memory._context(board) is None else None)
            if reason:
                excluded.append({**origin, "reason": reason, "overlap_keys": sorted(overlap)})
                continue
            state_id = memory.digest([row["fen"], row["history_uci"]])
            if state_id not in groups:
                saved = copy.deepcopy(row)
                saved.update(record_id="merged-" + state_id, candidates=[],
                             merge_provenance={"state_sha256": state_id, "source_records": [], "candidate_origins": {}})
                groups[state_id] = {"row": saved, "items": {}, "first": origin}
            group = groups[state_id]
            saved = group["row"]
            if row["family_id"] != saved["family_id"]:
                conflicts.append({"kind": "family_inconsistency", "state_sha256": state_id,
                    "kept": group["first"], "kept_family": saved["family_id"],
                    "held": origin, "held_family": row["family_id"]})
                continue
            saved["merge_provenance"]["source_records"].append(origin)
            mappings.append({**origin, "merged_record_id": saved["record_id"]})
            for item in row["candidates"]:
                uci = item["uci"]
                reference = _ref(info, row, item)
                if uci in group["items"]:
                    kept = group["items"][uci]
                    if memory.digest(kept) == memory.digest(item):
                        saved["merge_provenance"]["candidate_origins"][uci].append(reference)
                        duplicates.append({"state_sha256": state_id, "uci": uci, "origin": reference})
                    else:
                        conflicts.append({"kind": "candidate_evidence_conflict", "state_sha256": state_id, "uci": uci,
                            "kept": saved["merge_provenance"]["candidate_origins"][uci][0], "held": reference,
                            "differing_fields": sorted(key for key in set(kept) | set(item) if kept.get(key) != item.get(key))})
                    continue
                if len(saved["candidates"]) >= 8:
                    capacity.append({"state_sha256": state_id, "uci": uci, "origin": reference,
                                     "reason": "runtime_candidate_capacity_preserve_earlier_inputs"})
                    continue
                group["items"][uci] = copy.deepcopy(item)
                saved["candidates"].append(copy.deepcopy(item))
                saved["merge_provenance"]["candidate_origins"][uci] = [reference]
    records = [group["row"] for group in groups.values()]
    if not records:
        raise ValueError("No eligible records remain after the union of holdout restrictions")
    source_inputs = [entry["info"] for entry in loaded]
    artifact = {"schema": memory.SCHEMA, "policy": memory.POLICY, "status": "approved",
        "scope": "merged exact practiced full histories; structural approval only; every proposal still needs fresh runtime verification",
        "engine_identity": copy.deepcopy(identity), "heldout_family_ids": heldout_families,
        "heldout_position_keys": heldout_keys, "records": records,
        "source_inputs": source_inputs,
        "derived_source_inputs_sha256": memory.digest([{key: info[key] for key in ("input_id", "sha256", "memory_id", "source_sha256", "derived_source_inputs_sha256")} for info in source_inputs]),
        "derived_curriculum_inputs_sha256": memory.digest([{key: info[key] for key in ("input_id", "sha256", "curriculum_sha256", "derived_curriculum_inputs_sha256")} for info in source_inputs]),
        "merge_tool": {"policy": MERGE_POLICY, "source_sha256": _sha(Path(__file__).read_bytes()),
                       "loader_source_sha256": _sha(Path(memory.__file__).read_bytes())},
        "counts": {"input_memories": len(loaded), "input_records": sum(len(entry["value"]["records"]) for entry in loaded),
                   "memory_records": len(records), "memory_candidates": sum(len(row["candidates"]) for row in records),
                   "conflicts_held": len(conflicts), "duplicate_candidates": len(duplicates),
                   "excluded_rows": len(excluded), "capacity_held_candidates": len(capacity)},
        "acceptance": {"passed": True, "basis": "approved input identities, legal full-history replay, conservative conflict priority and holdout union; no new strength or soundness claim"}}
    artifact["memory_id"] = memory.memory_digest(artifact)
    memory._validate(artifact)
    artifact_blob = _json_bytes(artifact)
    if len(artifact_blob) > memory.MAX_BYTES:
        raise ValueError("Merged artifact exceeds the runtime loader size limit")
    def unchanged():
        return all(entry["path"].read_bytes() == entry["blob"] for entry in loaded)
    if not unchanged():
        raise ValueError("Memory input changed during the merge")
    report = {"schema": "regicide.gambit-memory-merge-report.v1", "policy": MERGE_POLICY,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "complete_with_held_conflicts" if conflicts else "complete",
        "memory_id": artifact["memory_id"], "operations_memory_sha256": _sha(artifact_blob),
        "counts": artifact["counts"], "conflicts": conflicts, "excluded_rows": excluded,
        "duplicate_candidates": duplicates, "capacity_held_candidates": capacity, "record_mapping": mappings,
        "base_input_id": source_inputs[0]["input_id"], "inputs": source_inputs,
        "conflict_rule": "Earlier input/record wins; entire candidate JSON is evidence, including query identity. Higher score/depth never replaces it.",
        "input_bytes_unchanged": True, "activated": False,
        "runtime_contract": "candidate_only=true and existing loss cap; fresh native runtime safety verification remains required"}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(exist_ok=False)
    (output / "inputs").mkdir()
    for entry in loaded:
        _write_new(output / entry["info"]["snapshot"], entry["blob"])
    _write_new(output / "inputs.json", _json_bytes({"schema": "regicide.gambit-memory-merge-inputs.v1", "inputs": source_inputs}))
    _write_new(output / "operations_memory.json", artifact_blob)
    if not unchanged():
        raise ValueError("Memory input changed before merge publication; no completion report written")
    # The report is the final completion marker. A failed publication may leave
    # a new partial output folder, but can never replace an existing file.
    _write_new(output / "merge-report.json", _json_bytes(report))
    return artifact


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=memory.DEFAULT_MEMORY)
    parser.add_argument("--input", dest="inputs", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True, help="Fresh directory; no automatic installation")
    args = parser.parse_args(argv)
    try:
        result = merge(args.base, args.inputs, args.output)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({"memory_id": result["memory_id"], **result["counts"], "activated": False}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
