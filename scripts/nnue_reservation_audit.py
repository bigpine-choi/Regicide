"""Read-only, portable audit of three immutable historical NNUE reservations.

This is evidence verification, never permission to seal, label, train or search.
The execution authenticators deliberately remain unchanged: changed live source
still invalidates their reservations. Only explicitly archived source bytes may
satisfy the historical bindings below; model/data bindings always use live files.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from zipfile import ZipFile

import nnue_tactical_search_gate as legacy

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = 'docs/checks/2026-10-22-nnue-tactical-search/gate-001'
SUPERSEDED = 'docs/checks/2026-10-22-nnue-qsearch-cost/gate-001'
ADOPTED = 'docs/checks/2026-10-22-nnue-qsearch-cost/gate-002'
MODEL = 'docs/checks/2026-10-22-nnue-head-repair/attempt-001/model.nnue'
FILES = frozenset(('reservation.json', 'frozen-roots.json', 'blocked-features.json',
                   'position-only-verification.json'))
# Exact original JSON bytes, including their original absolute path strings.
PINS = {
    ORIGINAL: ('bff49134b6317ab0977bb926c8be2eca379e7439b6f84d9bb1b7672ad3092451',
               '0db701173d63fd3a41cf2c452c10f0646d329c72af10c161f6a8c31b8f227222', 267),
    SUPERSEDED: ('c89ff92ec3eb78e245a8b6f6aea393dd8916af63b397f6e19ff0ff1deb1033cc',
                 '5401fb3aca4aa0dc9f6fc7b53b4667524dc8cd8c124e721d31d910372f6e9aa5', 292),
    ADOPTED: ('8ade02d18b546c9cd092c194c00486f6ed065c93bd60cc01ecfbcaff9fe07b05',
              'e29862bc2a8607a49807bfe1f393f19f207ad8f00222bcf701cac0f930aff040', 301),
}
ARCHIVED_SOURCES = {
    ('scripts/neural_training.py', '7ce2a4e8d3ccc9a4ffad1d48f12749b3dc831b51dbc39c2b7cc7a838852c9343'):
        'docs/checks/2026-10-22-nnue-head-repair/attempt-001/source-snapshot/scripts/neural_training.py',
    ('scripts/nnue_qsearch_cost_gate.py', 'bd9afe21ce53aced3c6e870f84f0667a27a0e3110ef6e027a3777e1e956bc63a'):
        'docs/checks/2026-10-22-nnue-qsearch-cost/pre-ordering-source-snapshot/scripts/nnue_qsearch_cost_gate.py',
    ('tests/nnue_qsearch_cost_gate_tests.py', 'c9300cf751f13bc3d0886665f636eea6fedd28134791451929fbe0dd54538a59'):
        'docs/checks/2026-10-22-nnue-qsearch-cost/pre-ordering-source-snapshot/tests/nnue_qsearch_cost_gate_tests.py',
}
SNAPSHOT = 'docs/checks/2026-11-06-gambit-creativity/ci-source-snapshot'
ZIP_SHA = '604e04b773786dc540cbf7ce1402da9b36a4165f6f75bbbd0e843e5518e249a8'
MANIFEST_SHA = 'd03db61c7a992d45d8bdd0a154528402cc224118cc6acb562f8a0c84c02bc8bb'
ZIP_SOURCES = {
    ('tests/nnue_qsearch_cost_gate_tests.py', 'e3b54e572fef1a66caa43a25c85136cce9db3a2679c0f465df3a8c601c85b8ab'),
}


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _sha(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def recorded_relative(path, recorded_root):
    """Interpret the recorded OS syntax, not the current machine's syntax."""
    kind = PureWindowsPath if PureWindowsPath(str(recorded_root)).is_absolute() else PurePosixPath
    old_root, old_path = kind(recorded_root), kind(path)
    _require(old_root.is_absolute() and old_path.is_absolute() and
             '..' not in old_root.parts and '..' not in old_path.parts,
             'Historical path must be absolute without traversal')
    try:
        relative = old_path.relative_to(old_root)
    except ValueError as error:
        raise ValueError('Historical path is outside recorded repository') from error
    _require(bool(relative.parts), 'Historical path names the repository itself')
    return PurePosixPath(*relative.parts).as_posix()


def _local(repository, relative):
    root = Path(repository).resolve()
    rel = PurePosixPath(relative)
    _require(not rel.is_absolute() and '..' not in rel.parts, 'Invalid relative artifact path')
    path = root.joinpath(*rel.parts).resolve()
    _require(path.is_relative_to(root), 'Artifact escapes current repository')
    return path


def _recorded_root(reservation):
    source = reservation['gate_source_path']
    kind = PureWindowsPath if PureWindowsPath(source).is_absolute() else PurePosixPath
    path = kind(source)
    _require(path.parts[-2:] == ('scripts', 'nnue_tactical_search_gate.py'),
             'Original gate source identity differs')
    return path.parents[1]


def _input_digest(repository, relative, expected):
    key = relative, expected
    if key in ARCHIVED_SOURCES:
        return _sha(_local(repository, ARCHIVED_SOURCES[key]))
    if key in ZIP_SOURCES:
        archive = _local(repository, SNAPSHOT + '/historical-tests.zip')
        manifest_path = _local(repository, SNAPSHOT + '/manifest.json')
        _require(_sha(archive) == ZIP_SHA and _sha(manifest_path) == MANIFEST_SHA,
                 'Historical Git source archive changed')
        manifest = _read(manifest_path)
        _require(any(row['path'] == relative and row['sha256'] == expected
                     for row in manifest['files']), 'Historical source manifest binding differs')
        with ZipFile(archive) as snapshot:
            return sha256(snapshot.read(relative)).hexdigest()
    return _sha(_local(repository, relative))


def _audit_one(repository, relative, recorded_root=None):
    directory = _local(repository, relative)
    _require({p.name for p in directory.iterdir()} == FILES,
             'Historical reservation was queried, sealed, consumed, or changed')
    reservation_sha, audit_sha, count = PINS[relative]
    _require(_sha(directory / 'reservation.json') == reservation_sha and
             _sha(directory / 'position-only-verification.json') == audit_sha,
             'Historical reservation or zero-query audit bytes changed')
    reservation = _read(directory / 'reservation.json')
    recorded_root = recorded_root or _recorded_root(reservation)
    _require(reservation['criteria'] == legacy.CRITERIA and
             reservation['native_nodes'] == legacy.previous.NODES and
             reservation['teacher_nodes'] == legacy.previous.TEACHER_NODES and
             reservation['teacher_depth'] == legacy.TEACHER_DEPTH,
             'Frozen criteria or budgets differ')
    _require(recorded_relative(reservation['model_path'], recorded_root) == MODEL and
             reservation['model_sha256'] == legacy.MODEL_SHA and
             _sha(_local(repository, MODEL)) == legacy.MODEL_SHA,
             'Frozen model identity or bytes differ')
    for name, key in (('frozen-roots.json', 'frozen_roots_sha256'),
                      ('blocked-features.json', 'blocked_features_sha256')):
        _require(_sha(directory / name) == reservation[key], 'Frozen position bytes differ: ' + name)
    _require(len(reservation['input_sha256']) == count, 'Historical input count differs')
    for path, expected in reservation['input_sha256'].items():
        rel = recorded_relative(path, recorded_root)
        _require(_input_digest(repository, rel, expected) == expected,
                 'Historical input bytes differ: ' + rel)
    audit = _read(directory / 'position-only-verification.json')
    _require(audit.get('completed') is True and audit.get('passed') is True and
             all(audit.get(key) == 0 for key in
                 ('native_queries', 'teacher_queries', 'training_runs', 'overlap_features')) and
             audit.get('reservation_sha256') == reservation_sha,
             'Historical zero-query audit differs')
    return directory, reservation, _read(directory / 'frozen-roots.json'), recorded_root


def audit_original(repository=ROOT):
    """Verify original evidence only; no execution gate imports this function."""
    directory, reservation, roots, _ = _audit_one(repository, ORIGINAL)
    blocked = set(_read(directory / 'blocked-features.json'))
    recomputed = set().union(*(legacy.root_features(legacy.source_board(row)) for row in roots))
    _require(len(roots) == 16 and len(blocked) == 594 and blocked == recomputed,
             'Original sixteen roots or legal children differ')
    return directory, reservation, roots, blocked


def audit_cost_source(repository=ROOT):
    """Verify the original, superseded and adopted historical evidence chain."""
    source, original, roots, blocked = audit_original(repository)
    recorded_root = _recorded_root(original)
    superseded, old, _, _ = _audit_one(repository, SUPERSEDED, recorded_root)
    previous, reservation, _, _ = _audit_one(repository, ADOPTED, recorded_root)
    for directory, value in ((superseded, old), (previous, reservation)):
        _require(recorded_relative(value['source_reservation'], recorded_root) == ORIGINAL and
                 value['source_reservation_sha256'] == PINS[ORIGINAL][0],
                 'Historical source chain differs')
        for name in ('frozen-roots.json', 'blocked-features.json'):
            _require((directory / name).read_bytes() == (source / name).read_bytes(),
                     'Historical adopted position bytes differ')
    _require(recorded_relative(reservation['superseded_unused_cache_only_reservation'],
                               recorded_root) == SUPERSEDED and
             reservation['superseded_reservation_sha256'] == PINS[SUPERSEDED][0],
             'Historical superseded chain differs')
    return source, original, previous, reservation, roots, blocked
