"""Run one frozen promotion comparison on a GitHub-hosted Windows runner.

This remote worker never changes a champion or learning-store counter. It
publishes evidence only; a separate local importer must independently verify it.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
import sys
import tempfile
import zipfile

import learning_arena as ARENA


SCHEMA = "regicide.learning.remote-request.v1"
RESULT_SCHEMA = "regicide.learning.remote-result.v1"
ROOT = Path(__file__).resolve().parents[1]
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_BINARY_BYTES = 16 * 1024 * 1024
SHA = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
ZERO_SHA = re.compile(r"0{40}(?:0{24})?\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          check=check, timeout=60).stdout


def json_write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def relative_path(root, value, *, must_exist=True):
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or any(character in value for character in "\x00\r\n")):
        raise ValueError("remote paths must be nonempty safe repository-relative POSIX paths")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in (".", "..") for part in pure.parts) or pure.as_posix() != value:
        raise ValueError("remote path is absolute, noncanonical or traverses its repository")
    root = Path(root).resolve()
    path = root.joinpath(*pure.parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError("remote path escapes repository")
    for current in (path, *path.parents):
        if current == root:
            break
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if current.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("remote paths may not contain symlinks, junctions or reparse points")
    if must_exist and not path.is_file():
        raise ValueError("remote input file is missing")
    return path


def source_inventory(root):
    root = Path(root)
    paths = []
    for pattern in ("scripts/**/*.py", "tests/**/*.py", "src/*.cpp", "include/regicide/*.hpp",
                    ".github/workflows/*.yml", ".github/workflows/*.yaml"):
        paths += [path for path in root.glob(pattern) if path.is_file()]
    paths += [root / "tests/data" / name for name in (
        "learning_openings_v1.json", "learning_arena_openings_v1.json", "endgame_learning_seeds_v1.json")]
    return {path.relative_to(root).as_posix() for path in paths}


def _positive(value, name, minimum):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _digest(value, name):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError(name + " must be a lowercase SHA256")


def _commit(root, value):
    if not isinstance(value, str) or not COMMIT.fullmatch(value):
        raise ValueError("full lowercase commit identity required")
    resolved = git(root, "rev-parse", "--verify", value + "^{commit}").decode().strip()
    if resolved != value:
        raise ValueError("commit identity does not resolve exactly")
    return resolved


def validate_request(root, request_path, expected_repository):
    root = Path(root).resolve()
    request_file = relative_path(root, request_path)
    raw = request_file.read_bytes()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("remote request exceeds2MiB")
    request = json.loads(raw.decode("utf-8"))
    if not isinstance(request, dict) or request.get("schema") != SCHEMA:
        raise ValueError("unexpected remote request schema")
    if not isinstance(expected_repository, str) or not REPOSITORY.fullmatch(expected_repository):
        raise ValueError("expected GitHub repository identity is required")
    if request.get("repository", expected_repository) != expected_repository:
        raise ValueError("remote request names another repository")
    _positive(request.get("attempt_index"), "attempt_index", 1)
    _positive(request.get("pairs"), "pairs", 32)
    _positive(request.get("movetime_ms"), "movetime_ms", 100)
    _positive(request.get("seed"), "seed", 0)
    if request.get("source_hash_basis") != "git_blob_bytes":
        raise ValueError("remote source hashes must use committed git_blob_bytes")
    implementation = _commit(root, request.get("implementation_commit"))
    head = git(root, "rev-parse", "HEAD").decode().strip()
    git(root, "merge-base", "--is-ancestor", implementation, head)
    prefix = request.get("output_prefix")
    relative_path(root, prefix, must_exist=False)
    if not prefix.startswith("docs/checks/") or PurePosixPath(prefix).name != "remote-result":
        raise ValueError("remote results must stay in a declared docs/checks remote-result prefix")
    frozen = request.get("source_files_sha256")
    if not isinstance(frozen, dict) or not source_inventory(root).issubset(frozen):
        raise ValueError("remote source freeze omits scripts, tests, core, fixtures or workflows")
    for relative, expected in frozen.items():
        _digest(expected, "source SHA256")
        path = relative_path(root, relative)
        blob = git(root, "show", implementation + ":" + relative)
        if sha(path) != expected or hashlib.sha256(blob).hexdigest() != expected:
            raise ValueError("remote source working/Git blob SHA256 mismatch: " + relative)
    for path_key, hash_key in (("binary_zip", "binary_zip_sha256"), ("candidate_model", "candidate_sha256")):
        path = relative_path(root, request.get(path_key))
        _digest(request.get(hash_key), hash_key)
        if sha(path) != request[hash_key]:
            raise ValueError("remote input SHA256 mismatch: " + path_key)
    _digest(request.get("binary_sha256"), "binary_sha256")
    champion_path, champion_hash = request.get("champion_model"), request.get("champion_sha256")
    if champion_path is None and champion_hash is None:
        pass
    elif champion_path is not None and champion_hash is not None:
        _digest(champion_hash, "champion_sha256")
        if sha(relative_path(root, champion_path)) != champion_hash:
            raise ValueError("remote champion SHA256 mismatch")
    else:
        raise ValueError("champion_model and champion_sha256 must both be null or both be present")
    prepared = request.get("prepared_run")
    if not isinstance(prepared, str) or not prepared or any(c in prepared for c in "\x00\r\n"):
        raise ValueError("prepared_run identity is required")
    return request, hashlib.sha256(raw).hexdigest(), head


def extract_binary(archive, destination, expected_sha):
    archive, destination = Path(archive), Path(destination)
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("binary ZIP exceeds bounded archive size")
    with zipfile.ZipFile(archive) as zipped:
        entries = zipped.infolist()
        if len(entries) != 1 or entries[0].filename != "regicide.exe":
            raise ValueError("binary ZIP must contain only the top-level regicide.exe")
        info = entries[0]
        mode = (info.external_attr >> 16) & 0xF000
        if (info.is_dir() or info.flag_bits & 1 or mode not in (0, stat.S_IFREG)
                or info.external_attr & 0x400 or not 2 <= info.file_size <= MAX_BINARY_BYTES
                or info.compress_size < 1 or info.file_size / info.compress_size > 100):
            raise ValueError("binary ZIP has unsupported type, encryption, reparse flags or expansion")
        with zipped.open(info) as handle:
            raw = handle.read(MAX_BINARY_BYTES + 1)
        if len(raw) != info.file_size or not raw.startswith(b"MZ") or hashlib.sha256(raw).hexdigest() != expected_sha:
            raise ValueError("unpacked Windows binary size, signature or SHA256 mismatch")
    destination.write_bytes(raw)
    return destination


def previous_commit(root, before):
    if before is not None and not ZERO_SHA.fullmatch(before):
        return _commit(root, before)
    probe = git(root, "rev-parse", "--verify", "HEAD^", check=False).decode().strip()
    return probe if COMMIT.fullmatch(probe) else None


def discover_request(root, before):
    previous = previous_commit(root, before)
    args = ("diff", "--name-only", "--diff-filter=AM", previous, "HEAD") if previous else (
        "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "HEAD")
    paths = [path for path in git(root, *args).decode("utf-8").splitlines()
             if PurePosixPath(path).name == "remote-request.json"]
    if len(paths) != 1:
        raise ValueError("push must add or modify exactly one remote-request.json")
    relative_path(root, paths[0])
    return paths[0]


def assert_fresh_request(root, before, request):
    previous = previous_commit(root, before)
    if previous is None:
        return
    paths = git(root, "ls-tree", "-r", "--name-only", previous).decode("utf-8").splitlines()
    for path in paths:
        if PurePosixPath(path).name != "remote-request.json":
            continue
        document = json.loads(git(root, "show", previous + ":" + path).decode("utf-8"))
        if document.get("schema") == SCHEMA and type(document.get("attempt_index")) is int:
            if document["attempt_index"] >= request["attempt_index"]:
                raise ValueError("remote attempt number was already requested; automatic retries are forbidden")


def assert_no_result_branch(root, attempt):
    found = git(root, "ls-remote", "--heads", "origin", f"refs/heads/validation/endgame-{attempt}-*")
    if found.strip():
        raise ValueError("a remote result already consumes this attempt; no retry is allowed")


def outputs(**values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            for key, value in values.items():
                if any(character in str(value) for character in "\x00\r\n"):
                    raise ValueError("invalid GitHub output")
                handle.write(f"{key}={value}\n")


def _host():
    return {"platform": platform.platform(), "machine": platform.machine(), "processor": platform.processor(),
            "logical_cpus": os.cpu_count(), "python": platform.python_version(),
            "github": {key: os.environ.get(key) for key in ("GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_RUN_ID",
                "GITHUB_RUN_ATTEMPT", "GITHUB_JOB", "GITHUB_WORKFLOW", "GITHUB_SERVER_URL", "RUNNER_OS", "RUNNER_ARCH")}}


def _content_manifest(directory):
    entries = [{"path": path.relative_to(directory).as_posix(), "sha256": sha(path), "bytes": path.stat().st_size}
               for path in sorted(directory.rglob("*")) if path.is_file() and path.name != "content-manifest.json"]
    json_write(directory / "content-manifest.json", {"schema": "regicide.learning.remote-content.v1", "files": entries,
        "scope": "all result bytes except this manifest itself; no executable or model adoption"})


def execute_request(root, request_path, before=None):
    root = Path(root).resolve()
    request, request_sha, head = validate_request(root, request_path, os.environ.get("GITHUB_REPOSITORY"))
    if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_OS") != "Windows"
            or os.environ.get("GITHUB_RUN_ATTEMPT") != "1" or os.environ.get("GITHUB_SHA") != head):
        raise ValueError("only the first run of the exact GitHub Windows checkout may execute a remote request")
    if discover_request(root, before) != request_path:
        raise ValueError("requested path is not this push's unique changed request")
    assert_fresh_request(root, before, request)
    assert_no_result_branch(root, request["attempt_index"])
    relative_output = request["output_prefix"] + "/" + request_sha[:12]
    directory = relative_path(root, relative_output, must_exist=False)
    directory.mkdir(parents=True, exist_ok=False)
    branch = f"validation/endgame-{request['attempt_index']}-{request_sha[:12]}"
    outputs(result_path=relative_output, result_branch=branch, request_sha256=request_sha)
    (directory / "request.json").write_bytes(relative_path(root, request_path).read_bytes())
    report = {"schema": RESULT_SCHEMA, "status": "running", "started_utc": ARENA.utc_now(),
        "request_path": request_path, "request_sha256": request_sha, "checked_out_commit": head,
        "implementation_commit": request["implementation_commit"], "prepared_run": request["prepared_run"],
        "attempt_index": request["attempt_index"], "result_branch": branch, "result_path": relative_output,
        "host": _host(), "arena_status": None, "errors": [], "promotion_performed": False,
        "scope": "remote hardware comparison; same exact executable and declared models; local adoption requires independent import verification"}
    try:
        with tempfile.TemporaryDirectory(prefix="regicide-remote-arena-") as temporary:
            binary = extract_binary(relative_path(root, request["binary_zip"]), Path(temporary) / "regicide.exe",
                                    request["binary_sha256"])
            champion = relative_path(root, request["champion_model"]) if request.get("champion_model") else None
            arena = ARENA.run_arena(binary, relative_path(root, request["candidate_model"]), champion,
                directory / "arena", pairs=request["pairs"], movetime_ms=request["movetime_ms"],
                seed=request["seed"], attempt_index=request["attempt_index"], stop_when_impossible=True)
            report["arena_status"] = arena["status"]
            if arena["status"] == "complete":
                ARENA.verify_match_records(arena)
            elif arena["status"] == "rejected_early_futile":
                ARENA.verify_early_rejection(arena)
            else:
                raise ValueError("remote arena incomplete, contaminated or failed")
            after, after_sha, after_head = validate_request(root, request_path, os.environ["GITHUB_REPOSITORY"])
            if after != request or after_sha != request_sha or after_head != head or sha(binary) != request["binary_sha256"]:
                raise ValueError("remote request, checkout or binary changed during validation")
            report.update(status="complete", decision=arena["decision"],
                          valid_for_rejection=arena.get("valid_for_rejection", False))
    except (Exception, KeyboardInterrupt) as error:
        report.update(status="failed")
        report["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        report["finished_utc"] = ARENA.utc_now()
        json_write(directory / "remote-result.json", report)
        _content_manifest(directory)
    return report


def publish_result(root, result_path):
    """Commit only the unique result directory, on a new child of request HEAD."""
    root = Path(root).resolve()
    directory = relative_path(root, result_path, must_exist=False)
    report = json.loads((directory / "remote-result.json").read_text(encoding="utf-8"))
    if report.get("schema") != RESULT_SCHEMA or report.get("status") not in ("complete", "failed"):
        raise ValueError("only terminal remote evidence may be published")
    if report["result_path"] != result_path or git(root, "rev-parse", "HEAD").decode().strip() != report["checked_out_commit"]:
        raise ValueError("remote result branch must directly descend from its request checkout")
    if os.environ.get("GITHUB_SHA") != report["checked_out_commit"]:
        raise ValueError("result is not bound to this GitHub request run")
    if sha(directory / "request.json") != report["request_sha256"]:
        raise ValueError("published request bytes changed")
    request = json.loads((directory / "request.json").read_text())
    expected_branch = f"validation/endgame-{request['attempt_index']}-{report['request_sha256'][:12]}"
    if (report["result_branch"] != expected_branch or report["attempt_index"] != request["attempt_index"]
            or result_path != request["output_prefix"] + "/" + report["request_sha256"][:12]):
        raise ValueError("remote publication namespace differs from its frozen request")
    manifest = json.loads((directory / "content-manifest.json").read_text())
    expected = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    actual = {path.relative_to(directory).as_posix(): sha(path) for path in directory.rglob("*")
              if path.is_file() and path.name != "content-manifest.json"}
    if actual != expected:
        raise ValueError("remote result content manifest changed")
    if git(root, "diff", "--cached", "--name-only").strip():
        raise ValueError("preexisting staged files cannot enter result publication")
    assert_no_result_branch(root, report["attempt_index"])
    git(root, "switch", "--create", report["result_branch"], report["checked_out_commit"])
    git(root, "add", "--", result_path)
    staged = git(root, "diff", "--cached", "--name-only").decode().splitlines()
    if not staged or any(not path.startswith(result_path + "/") for path in staged):
        raise ValueError("result publication attempted to stage non-result files")
    git(root, "-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit", "-m", f"Record remote arena attempt {report['attempt_index']} ({report['request_sha256'][:12]})")
    git(root, "push", "origin", "HEAD:refs/heads/" + report["result_branch"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("discover", "run", "publish"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--request")
    parser.add_argument("--before")
    parser.add_argument("--result")
    args = parser.parse_args(argv)
    try:
        if args.mode == "discover":
            outputs(request=discover_request(args.root, args.before))
            return 0
        if args.mode == "publish":
            publish_result(args.root, args.result)
            return 0
        result = execute_request(args.root, args.request, args.before)
        return 0 if result["status"] == "complete" else 1
    except Exception as error:
        fallback = args.root / "build/remote-arena-failure"
        fallback.mkdir(parents=True, exist_ok=True)
        json_write(fallback / "failure.json", {"schema": RESULT_SCHEMA, "status": "failed",
            "error": f"{type(error).__name__}: {error}", "host": _host(), "promotion_performed": False})
        print(f"remote arena failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
