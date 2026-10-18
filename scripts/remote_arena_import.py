"""Verify authenticated remote evidence before the unchanged local model gate.

The caller obtains the workflow receipt from trusted GitHub API responses and
checks the current study/catalog freeze first. This module additionally binds
that receipt to existing Git objects and every result byte. It never fetches,
retries, trains, or accepts a shortened arena as a promotion result.
"""

import copy
import hashlib
import json
from pathlib import Path, PureWindowsPath
import shutil

import collect_learning_data as DATA
import learning_arena as ARENA
import learning_cycle as CYCLE
import learning_registry as REGISTRY
import remote_learning_arena as REMOTE

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "bigpine-choi/Regicide"
WORKFLOW = ".github/workflows/remote-learning-arena.yml"
RECEIPT_SCHEMA = "regicide.learning.remote-workflow-receipt.v1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    require(isinstance(value, dict), "remote JSON must contain an object")
    return value


def _inventory(directory):
    original = Path(directory)
    require(not original.is_symlink() and not (getattr(original.lstat(), "st_file_attributes", 0) & 0x400),
            "remote evidence directory is a link or reparse point")
    directory = original.resolve(strict=True)
    files, folded = {}, set()
    for path in directory.rglob("*"):
        require(not path.is_symlink() and not (getattr(path.lstat(), "st_file_attributes", 0) & 0x400),
                "remote evidence contains a link or reparse point")
        require(path.resolve().is_relative_to(directory), "remote evidence escaped its directory")
        if path.is_file():
            relative = path.relative_to(directory).as_posix()
            REMOTE.relative_path(directory, relative)
            require(relative.casefold() not in folded, "remote evidence has case-alias duplicate paths")
            folded.add(relative.casefold())
            files[relative] = {"sha256": DATA.sha256_file(path), "bytes": path.stat().st_size}
        else:
            require(path.is_dir(), "remote evidence contains a nonregular entry")
    return files


def _content(directory):
    files = _inventory(directory)
    manifest = read_json(Path(directory) / "content-manifest.json")
    require(manifest.get("schema") == "regicide.learning.remote-content.v1" and
            isinstance(manifest.get("files"), list), "invalid remote content manifest")
    expected = {}
    for entry in manifest["files"]:
        require(isinstance(entry, dict), "invalid remote content entry")
        path = entry.get("path")
        REMOTE.relative_path(directory, path)
        require(path != "content-manifest.json" and path not in expected and
                type(entry.get("bytes")) is int and entry["bytes"] >= 0 and
                isinstance(entry.get("sha256"), str) and REMOTE.SHA.fullmatch(entry["sha256"]),
                "duplicate or malformed remote content entry")
        expected[path] = {"sha256": entry["sha256"], "bytes": entry["bytes"]}
    require({name: value for name, value in files.items() if name != "content-manifest.json"} == expected,
            "remote content files, lengths or SHA256 differ from their manifest")
    return files


def _git_binding(request, request_relative, request_raw, result, receipt, files, directory):
    head, commit = receipt["head_sha"], receipt["result_commit"]
    for value in (head, commit):
        require(isinstance(value, str) and REMOTE.COMMIT.fullmatch(value), "full Git commit identity required")
        require(REMOTE.git(ROOT, "rev-parse", "--verify", value + "^{commit}").decode().strip() == value,
                "remote receipt Git commit does not exist exactly")
    parents = REMOTE.git(ROOT, "show", "-s", "--format=%P", commit).decode().strip().split()
    require(parents == [head], "remote result commit is not a direct single-parent child of request HEAD")
    require(REMOTE.git(ROOT, "show", head + ":" + request_relative) == request_raw,
            "workflow request HEAD does not contain the frozen request bytes")
    REMOTE.git(ROOT, "merge-base", "--is-ancestor", request["implementation_commit"], head)
    source_hashes = request.get("source_files_sha256")
    require(isinstance(source_hashes, dict) and REMOTE.source_inventory(ROOT).issubset(source_hashes),
            "request source freeze omits implementation or workflow files")
    for relative, expected in source_hashes.items():
        REMOTE.relative_path(ROOT, relative)
        for revision in (request["implementation_commit"], head):
            require(digest(REMOTE.git(ROOT, "show", revision + ":" + relative)) == expected,
                    "workflow HEAD or implementation Git source blob changed")
    for relative, expected in request["payload_sha256"].items():
        require(digest(REMOTE.git(ROOT, "show", head + ":" + relative)) == expected,
                "workflow HEAD payload Git blob changed")
    prefix = result["result_path"] + "/"
    changed = REMOTE.git(ROOT, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).decode().splitlines()
    require(changed and all(path.startswith(prefix) for path in changed), "result commit changes non-result files")
    entries = REMOTE.git(ROOT, "ls-tree", "-r", "-z", commit, "--", result["result_path"])
    tree = {}
    for raw in entries.split(b"\0"):
        if not raw:
            continue
        header, name = raw.split(b"\t", 1)
        mode, kind, blob = header.decode("ascii").split()
        name = name.decode("utf-8")
        require(mode in ("100644", "100755") and kind == "blob" and name.startswith(prefix),
                "remote Git tree contains a nonregular result entry")
        relative = name[len(prefix):]
        require(relative not in tree, "duplicate remote Git tree path")
        tree[relative] = blob
    require(set(tree) == set(files), "remote Git result tree differs from the supplied evidence inventory")
    for relative, blob in tree.items():
        raw = REMOTE.git(ROOT, "cat-file", "blob", blob)
        require(len(raw) == files[relative]["bytes"] and digest(raw) == files[relative]["sha256"] and
                raw == (Path(directory) / relative).read_bytes(), "remote Git blob bytes differ: " + relative)


def _resource_artifacts(metadata, directory, request):
    require(metadata.get("binary_sha256") == request["binary_sha256"] and
            metadata.get("candidate_sha256") == request["candidate_sha256"] and
            metadata.get("champion_sha256") == request["champion_sha256"], "arena model or engine identity changed")
    require(metadata.get("opening_reserve") == ARENA.load_openings(ARENA.DEFAULT_OPENINGS, request["pairs"], request["seed"]),
            "remote opening schedule differs from the frozen request")
    engines = metadata.get("engines", {})
    require(set(engines) == {"baseline", "candidate"}, "remote arena needs exactly two declared engine roles")
    for role, key, name in (("candidate", "candidate_sha256", "candidate.nn"), ("baseline", "champion_sha256", "champion.nn")):
        identity = engines[role]
        expected_options = {**DATA.OPTIONS, "UseNeuralEvaluation": request[key] is not None}
        if request[key] is not None:
            expected_options["EvalFile"] = identity.get("options", {}).get("EvalFile")
        require(identity.get("binary_sha256") == request["binary_sha256"] and identity.get("model_sha256") == request[key]
                and identity.get("options") == expected_options, "remote engine options or model activation changed")
        if request[key] is not None:
            require(DATA.sha256_file(Path(directory) / name) == request[key], "remote model snapshot changed")
            validated = metadata.get("native_model_validation", {}).get(role)
            if role == "baseline":
                validated = metadata.get("native_model_validation", {}).get("baseline")
            require(isinstance(validated, dict) and validated.get("model_sha256") == request[key] and
                    type(validated.get("score_cp")) is int, "remote native model validation is missing")
    pgn, samples = Path(directory) / "games.pgn", Path(directory) / "resource-samples.jsonl"
    require(DATA.sha256_file(pgn) == metadata.get("pgn_sha256") and
            DATA.sha256_file(samples) == metadata.get("resource_samples_sha256"), "remote PGN or resource SHA256 changed")
    require(pgn.read_text(encoding="utf-8") == ARENA._pgn(metadata), "remote PGN differs from the recorded actual histories")
    rows = [json.loads(line) for line in samples.read_text(encoding="utf-8").splitlines()]
    guard = metadata.get("resource_guard", {})
    transports, owned = guard.get("transport_engines", {}), guard.get("owned_engines", [])
    require(len(rows) >= 2 and len(rows) == guard.get("snapshot_count") and not guard.get("contaminated") and
            guard.get("external_cpu_increase_ticks") == 0 and not guard.get("observed_external_engine_cpu_increase") and
            guard.get("failure") is None and set(transports) == {"baseline", "candidate"} and len(owned) == 2,
            "remote resource identity or coverage is incomplete/dirty")
    require({item.get("pid") for item in owned} == {transports[role].get("pid") for role in transports} and
            len({item.get("pid") for item in owned}) == 2, "remote transport ownership disagrees")
    for row in rows:
        require(isinstance(row, dict) and row.get("contaminated") is False and row.get("external_cpu_delta_ticks") == 0,
                "remote raw resource sample is contaminated")
        for field in ("gap_seconds", "snapshot_duration_seconds"):
            value = row.get(field)
            require(type(value) in (int, float) and 0 <= value <= 2, "remote resource sampling gap/duration failed")


def verify_and_import(run, state, binary, report, results_directory, workflow_receipt):
    run, state, binary = Path(run).resolve(), Path(state).resolve(), Path(binary).resolve()
    require(run.is_relative_to(state), "study run must remain inside the learning state")
    directory = Path(results_directory)
    receipt_path = Path(workflow_receipt) if isinstance(workflow_receipt, (str, Path)) else None
    receipt_raw = receipt_path.read_bytes() if receipt_path else None
    receipt = json.loads(receipt_raw) if receipt_path else copy.deepcopy(workflow_receipt)
    require(isinstance(receipt, dict) and receipt.get("schema") == RECEIPT_SCHEMA and
            receipt.get("repository") == REPOSITORY and receipt.get("workflow_path") == WORKFLOW and
            receipt.get("event") == "push" and receipt.get("status") == "completed" and
            receipt.get("conclusion") == "success" and str(receipt.get("run_attempt")) == "1" and
            str(receipt.get("run_id", "")).isdigit() and int(receipt["run_id"]) > 0,
            "workflow receipt is not a successful first exact Windows workflow run")
    reference = report["remote_request"]
    request_path = REMOTE.relative_path(ROOT, reference["path"])
    request_raw = request_path.read_bytes()
    require(digest(request_raw) == reference["sha256"], "local frozen remote request changed")
    request = json.loads(request_raw)
    require(request.get("schema") == REMOTE.SCHEMA and request.get("repository", REPOSITORY) == REPOSITORY and
            request.get("attempt_index") == report["attempt_index"] and type(request["attempt_index"]) is int and
            request.get("pairs") == 32 and request.get("movetime_ms") == 100 and
            type(request["pairs"]) is int and type(request["movetime_ms"]) is int and
            request.get("seed") == reference["seed"] and type(request["seed"]) is int and request["seed"] >= 0 and
            request.get("prepared_run") == run.relative_to(state).as_posix() and
            request.get("implementation_commit") == report["checkout"]["commit"] and
            request.get("source_hash_basis") == "git_blob_bytes", "remote request parameters differ from the prepared study")
    REMOTE.relative_path(ROOT, request.get("output_prefix"), must_exist=False)
    require(request["output_prefix"].startswith("docs/checks/") and request["output_prefix"].endswith("/remote-result"),
            "remote request output namespace changed")
    frozen = {request_path: digest(request_raw), binary: DATA.sha256_file(binary)}
    for relative, expected in reference["payload_sha256"].items():
        path = REMOTE.relative_path(ROOT, relative)
        require(DATA.sha256_file(path) == expected, "local exported payload changed")
        frozen[path] = expected
    require(reference["payload_sha256"].get(reference["path"]) == reference["sha256"], "request is absent from frozen payload")
    for field, hash_field in (("binary_zip", "binary_zip_sha256"), ("candidate_model", "candidate_sha256")):
        require(reference["payload_sha256"].get(request[field]) == request[hash_field], "payload body hash changed")
    candidate = CYCLE.contained(run, report["candidate"]["model_path"])
    require(DATA.sha256_file(binary) == report["binary_sha256"] == request["binary_sha256"] and
            DATA.sha256_file(candidate) == report["candidate"]["model_sha256"] == request["candidate_sha256"] and
            request.get("champion_sha256") == report["previous_model_sha256"], "local frozen engine/model/champion identity changed")
    frozen[candidate] = request["candidate_sha256"]
    old_champion = REGISTRY.load_champion(state, binary)
    require((old_champion["model_sha256"] if old_champion else None) == request["champion_sha256"], "previous champion changed before import")
    if old_champion:
        require(reference["payload_sha256"].get(request.get("champion_model")) == request["champion_sha256"], "champion payload is not frozen")
        frozen[Path(old_champion["model_path"])] = old_champion["model_sha256"]
        frozen[state / "champion.json"] = DATA.sha256_file(state / "champion.json")
    else:
        require(request.get("champion_model") is None, "unexpected champion payload")
    files = _content(directory)
    expected_files = {"content-manifest.json", "request.json", "remote-result.json", "arena/arena.json",
                      "arena/candidate.nn", "arena/games.pgn", "arena/resource-samples.jsonl"}
    if old_champion:
        expected_files.add("arena/champion.nn")
    require(set(files) == expected_files, "remote result contains missing or unexpected contract files")
    require((directory / "request.json").read_bytes() == request_raw, "remote result request bytes changed")
    result = read_json(directory / "remote-result.json")
    result_path = request["output_prefix"] + "/" + reference["sha256"][:12]
    expected_branch = f"validation/endgame-{report['attempt_index']}-{reference['sha256'][:12]}"
    require(reference["expected_result_branch"] == expected_branch == result.get("result_branch") == receipt.get("result_branch") and
            result.get("schema") == REMOTE.RESULT_SCHEMA and result.get("status") == "complete" and not result.get("errors") and
            result.get("promotion_performed") is False and result.get("request_path") == reference["path"] and
            result.get("request_sha256") == reference["sha256"] and result.get("result_path") == result_path and
            result.get("checked_out_commit") == receipt.get("head_sha") and
            result.get("implementation_commit") == request["implementation_commit"] and
            result.get("prepared_run") == request["prepared_run"] and result.get("attempt_index") == request["attempt_index"],
            "remote result provenance differs from request or trusted workflow receipt")
    github = result.get("host", {}).get("github", {})
    expected_github = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_SHA": receipt["head_sha"],
                       "GITHUB_RUN_ID": str(receipt["run_id"]), "GITHUB_RUN_ATTEMPT": "1",
                       "GITHUB_SERVER_URL": "https://github.com", "RUNNER_OS": "Windows"}
    require(all(github.get(key) == value for key, value in expected_github.items()), "remote host GitHub facts differ from trusted workflow receipt")
    git_request = {**request, "payload_sha256": reference["payload_sha256"]}
    _git_binding(git_request, reference["path"], request_raw, result, receipt, files, directory)
    arena = read_json(directory / "arena/arena.json")
    require(arena.get("attempt_index") == request["attempt_index"] and arena.get("requested_pairs") == request["pairs"] and
            arena.get("movetime_ms") == request["movetime_ms"] and arena.get("opening_reserve", {}).get("seed") == request["seed"] and
            arena.get("status") == result.get("arena_status") and arena.get("decision") == result.get("decision"),
            "remote arena request or result summary changed")
    original_artifact = PureWindowsPath(arena.get("artifact_directory", ""))
    require(original_artifact.is_absolute() and ".." not in original_artifact.parts, "original remote arena path is not absolute Windows provenance")
    tail = (*result_path.split("/"), "arena")
    require(tuple(part.casefold() for part in original_artifact.parts[-len(tail):]) == tuple(part.casefold() for part in tail),
            "original remote artifact path differs from its declared result namespace")
    relocations = {"artifact_directory": {"old": arena["artifact_directory"], "new": str(run / "arena")}}
    for role, key, filename in (("candidate", "candidate_sha256", "candidate.nn"), ("baseline", "champion_sha256", "champion.nn")):
        if request[key] is not None:
            value = arena.get("engines", {}).get(role, {}).get("options", {}).get("EvalFile")
            require(isinstance(value, str) and PureWindowsPath(value) == original_artifact / filename,
                    "remote EvalFile does not point to its exact recorded snapshot")
            relocations[f"engines.{role}.options.EvalFile"] = {"old": value, "new": str(run / "arena" / filename)}
    _resource_artifacts(arena, directory / "arena", request)
    if arena.get("status") == "complete":
        decision = ARENA.verify_match_records(arena)
        require(arena.get("eligible") == decision["eligible"] and arena.get("promotion_passed") == decision["passed"], "remote full gate flags changed")
    elif arena.get("status") == "rejected_early_futile":
        ARENA.verify_early_rejection(arena)
        require(result.get("valid_for_rejection") is True, "remote early rejection is not explicitly valid")
        decision = arena["decision"]
    else:
        raise ValueError("failed or dirty remote arena cannot be imported")
    evidence, localized = run / "remote-evidence", run / "arena"
    require(not evidence.exists() and not localized.exists() and not (run / "remote-import.json").exists(), "remote import refuses to overwrite earlier evidence")
    shutil.copytree(directory, evidence)
    require(_inventory(evidence) == files, "preserved remote evidence bytes changed during copy")
    shutil.copytree(evidence / "arena", localized)
    relocated = copy.deepcopy(arena)
    relocated["artifact_directory"] = str(localized)
    for role, key, name in (("candidate", "candidate_sha256", "candidate.nn"), ("baseline", "champion_sha256", "champion.nn")):
        if request[key] is not None:
            relocated["engines"][role]["options"]["EvalFile"] = str(localized / name)
    DATA.checkpoint_json(localized / "arena.json", relocated)
    for path in (evidence / "arena").iterdir():
        if path.name != "arena.json":
            require(path.read_bytes() == (localized / path.name).read_bytes(), "localization changed a non-JSON arena byte")
    if relocated["status"] == "complete":
        ARENA.verify_match_records(relocated)
    else:
        ARENA.verify_early_rejection(relocated)
    audit = {"schema": "regicide.learning.remote-import.v1", "status": "verified_evidence",
             "request_sha256": reference["sha256"], "workflow_receipt": receipt, "remote_files": files,
             "original_arena_json_sha256": files["arena/arena.json"]["sha256"],
             "localized_arena_json_sha256": DATA.sha256_file(localized / "arena.json"),
             "path_only_relocations": relocations, "promotion_policy": ARENA.POLICY}
    DATA.checkpoint_json(run / "remote-import.json", audit)
    if receipt_path:
        frozen[receipt_path] = digest(receipt_raw)
    for path, expected in frozen.items():
        require(DATA.sha256_file(path) == expected, "local frozen input changed during import")
    require(_inventory(directory) == files and _inventory(evidence) == files, "remote evidence changed before final local gate")
    current = REGISTRY.load_champion(state, binary)
    require((current["model_sha256"] if current else None) == request["champion_sha256"], "previous champion changed immediately before local gate")
    from endgame_study import verify_frozen
    verify_frozen(report, run)
    CYCLE.catalog_read(state)
    fields = {"arena": {"status": relocated["status"], "decision": decision,
                         "valid_for_rejection": relocated.get("valid_for_rejection", False),
                         "hardware_scope": "GitHub Windows runner; exact same executable/models, independent from local hardware"},
              "remote_import": {"receipt_run_id": str(receipt["run_id"]), "result_commit": receipt["result_commit"],
                                "audit": "remote-import.json", "original_evidence": "remote-evidence"},
              "adopted": False, "selected_model_sha256": report["previous_model_sha256"]}
    if relocated["status"] == "complete" and decision["passed"]:
        selected = REGISTRY.promote(state, binary, candidate, relocated, expected_champion_sha=report["previous_model_sha256"])
        fields.update(adopted=True, selected_model_sha256=selected["model_sha256"], generation=selected["generation"])
    else:
        fields["retained_reason"] = "Authenticated remote candidate failed the unchanged full gate or was mathematically unable to pass it"
    return fields
