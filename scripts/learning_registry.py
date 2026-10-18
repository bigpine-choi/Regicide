"""Atomic immutable model registry; only replayed passing arenas may promote.

The cycle owns the enclosing exclusive state lock. No CLI accepts an override or
cached pass flag. Source model bytes and the current champion are compared again
immediately before atomically replacing champion.json.
"""

import json
import os
from pathlib import Path
import re
import tempfile

from learning_arena import (DEFAULT_OPENINGS, _pgn, load_openings, sha256, utc_now,
                            validate_native_model, verify_match_records)

SCHEMA = "regicide.learning.champion.v1"
SHA = re.compile(r"[0-9a-f]{64}\Z")


def _atomic_json(path, document):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                     prefix="." + path.name + ".", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _inside(state, relative):
    if not isinstance(relative, str) or "\\" in relative:
        raise ValueError("model path must be a relative POSIX path within the state models directory")
    value = Path(relative)
    if value.is_absolute() or len(value.parts) != 2 or value.parts[0] != "models" or ".." in value.parts:
        raise ValueError("model path escapes the registry models directory")
    target = (state / value).resolve()
    if target.parent != (state / "models").resolve() or not target.is_relative_to(state):
        raise ValueError("model path resolves outside the state directory")
    # A directory symlink could make an apparently relative registry external.
    if (state / "models").is_symlink() or (state / value).is_symlink():
        raise ValueError("registry models cannot use symbolic links")
    return target


def load_champion(state, binary):
    state, binary = Path(state).resolve(), Path(binary).resolve()
    path = state / "champion.json"
    if not path.exists():
        return None  # The weak bootstrap model is deliberately never installed as champion.
    if path.is_symlink():
        raise ValueError("champion manifest cannot be a symbolic link")
    champion = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(champion, dict) or champion.get("schema") != SCHEMA:
        raise ValueError("unknown champion manifest schema")
    if (isinstance(champion.get("generation"), bool) or not isinstance(champion.get("generation"), int)
            or champion["generation"] < 1 or not isinstance(champion.get("model_sha256"), str)
            or not SHA.fullmatch(champion["model_sha256"])
            or not isinstance(champion.get("engine_sha256"), str)
            or not SHA.fullmatch(champion["engine_sha256"])):
        raise ValueError("invalid champion identity")
    model = _inside(state, champion.get("relative_model_path"))
    if sha256(model) != champion["model_sha256"]:
        raise ValueError("champion model SHA256 mismatch")
    if sha256(binary) != champion["engine_sha256"]:
        raise ValueError("champion belongs to a different engine binary; revalidation is required")
    validate_native_model(binary, model)
    return dict(champion, model_path=str(model))


def _read_arena(arena_result):
    if isinstance(arena_result, (str, Path)):
        path = Path(arena_result).resolve()
        metadata = json.loads(path.read_text(encoding="utf-8"))
        if Path(metadata.get("artifact_directory", "")).resolve() != path.parent:
            raise ValueError("arena artifact directory does not match its manifest location")
    elif isinstance(arena_result, dict):
        metadata = arena_result
        path = Path(metadata.get("artifact_directory", "")).resolve() / "arena.json"
        if not path.is_file() or json.loads(path.read_text(encoding="utf-8")) != metadata:
            raise ValueError("arena report is missing or differs from its saved evidence")
    else:
        raise ValueError("promotion requires a saved complete arena report")
    return metadata, path.parent


def _verify_artifacts(metadata, directory):
    if metadata.get("status") != "complete" or metadata.get("movetime_ms", 0) < 100:
        raise ValueError("promotion requires complete games at >=100 ms per move")
    reserve = metadata["opening_reserve"]
    expected = load_openings(DEFAULT_OPENINGS, metadata["requested_pairs"], reserve["seed"])
    if reserve != expected:
        raise ValueError("arena reserve or opening schedule differs from the fixed held-out policy")
    decision = verify_match_records(metadata)
    if not decision["passed"] or not metadata.get("promotion_passed") or not metadata.get("eligible"):
        raise ValueError("candidate did not pass the strict promotion criterion")
    for role, key in (("candidate", "candidate_sha256"), ("baseline", "champion_sha256")):
        identity = metadata["engines"][role]
        if identity.get("binary_sha256") != metadata["binary_sha256"] or identity.get("model_sha256") != metadata[key]:
            raise ValueError("arena engine identity mismatch")
        options = identity.get("options", {})
        from collect_learning_data import OPTIONS
        if any(options.get(name) != value for name, value in OPTIONS.items()):
            raise ValueError("arena engine search options were altered")
        if options.get("UseNeuralEvaluation") is not (metadata[key] is not None):
            raise ValueError("arena model activation does not match its declared identity")
        if metadata[key] is not None:
            snapshot = directory / ("candidate.nn" if role == "candidate" else "champion.nn")
            if Path(options.get("EvalFile", "")).resolve() != snapshot.resolve():
                raise ValueError("arena EvalFile does not point to the recorded immutable model snapshot")
    pgn = directory / "games.pgn"
    samples = directory / "resource-samples.jsonl"
    if sha256(pgn) != metadata.get("pgn_sha256") or sha256(samples) != metadata.get("resource_samples_sha256"):
        raise ValueError("arena PGN or resource evidence hash mismatch")
    if pgn.read_text(encoding="utf-8") != _pgn(metadata):
        raise ValueError("arena PGN does not match its full saved histories")
    rows = [json.loads(line) for line in samples.read_text(encoding="utf-8").splitlines()]
    guard = metadata["resource_guard"]
    if (len(rows) < 2 or len(rows) != guard.get("snapshot_count")
            or guard.get("external_cpu_increase_ticks") != 0
            or guard.get("observed_external_engine_cpu_increase")
            or guard.get("failure") is not None
            or set(guard.get("transport_engines", {})) != {"baseline", "candidate"}
            or len(guard.get("owned_engines", [])) != 2):
        raise ValueError("resource guard identity or coverage evidence is incomplete")
    for row in rows:
        if (row.get("contaminated") or row.get("external_cpu_delta_ticks") != 0
                or row.get("gap_seconds", 10) > 2 or row.get("snapshot_duration_seconds", 10) > 2):
            raise ValueError("resource sampling evidence is contaminated")
    for key, name in (("candidate_sha256", "candidate.nn"), ("champion_sha256", "champion.nn")):
        if metadata[key] is not None and sha256(directory / name) != metadata[key]:
            raise ValueError("immutable arena model snapshot changed")
    return decision


def promote(state, binary, candidate, arena_result, expected_champion_sha):
    state, binary, candidate = Path(state).resolve(), Path(binary).resolve(), Path(candidate).resolve()
    state.mkdir(parents=True, exist_ok=True)
    champion = load_champion(state, binary)
    previous_sha = champion["model_sha256"] if champion else None
    if previous_sha != expected_champion_sha:
        raise ValueError("champion changed before promotion; compare-and-swap rejected")
    metadata, directory = _read_arena(arena_result)
    decision = _verify_artifacts(metadata, directory)
    candidate_sha, binary_sha = sha256(candidate), sha256(binary)
    if (metadata.get("candidate_sha256") != candidate_sha or metadata.get("binary_sha256") != binary_sha
            or metadata.get("champion_sha256") != previous_sha):
        raise ValueError("arena identities do not match candidate, engine and current champion")
    if candidate_sha == previous_sha:
        raise ValueError("candidate is identical to the current champion")
    if champion and metadata["attempt_index"] <= champion.get("arena_attempt_index", 0):
        raise ValueError("arena attempt index must advance beyond the previous promotion")
    validate_native_model(binary, candidate)
    models = state / "models"
    models.mkdir(exist_ok=True)
    generation = champion["generation"] + 1 if champion else 1
    relative = f"models/{generation:06d}-{candidate_sha}.nn"
    destination = _inside(state, relative)
    # Unique, exclusive publication: an interrupted earlier attempt is reusable
    # only when its exact bytes already match the validated candidate.
    content = candidate.read_bytes()
    if __import__("hashlib").sha256(content).hexdigest() != candidate_sha:
        raise ValueError("candidate changed while preparing immutable model snapshot")
    with tempfile.NamedTemporaryFile(mode="wb", dir=models, prefix=".model.", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if sha256(destination) != candidate_sha:
                raise ValueError("immutable registry model file already exists with different bytes")
    finally:
        temporary.unlink(missing_ok=True)
    validate_native_model(binary, destination)
    current = load_champion(state, binary)
    if (current["model_sha256"] if current else None) != expected_champion_sha or sha256(binary) != binary_sha:
        raise ValueError("champion or engine changed during promotion; previous champion retained")
    document = {"schema": SCHEMA, "generation": generation, "relative_model_path": relative,
                "model_sha256": candidate_sha, "engine_sha256": binary_sha,
                "previous_model_sha256": previous_sha, "promoted_utc": utc_now(),
                "arena_sha256": sha256(directory / "arena.json"),
                "arena_attempt_index": metadata["attempt_index"], "decision": decision,
                "scope": "Passed reserved paired arena at declared time control; not a universal strength or Elo claim"}
    _atomic_json(state / "champion.json", document)
    # Everything that can fail validation is complete before the commit point.
    # A fresh native process or disk read here could report failure after the
    # champion had already changed. Return the already verified identity only.
    return dict(document, model_path=str(destination))
