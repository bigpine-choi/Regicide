"""Freeze literal engine identities and files without starting or configuring UCI.

The match runner retains advertised-option validation. A verified Git object or
copied build receipt is evidence supplied by the caller, not proof that an
executable was produced from that object. External source identity is declared.
Only declared files are frozen; an interpreter's base installation, libraries
or file arguments omitted from artifacts remain environment dependencies.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from urllib.parse import urlsplit

from compare_engines import parse_options, verified_commit


REQUIRED_OPTIONS = (
    'UseNeuralEvaluation', 'UseNNUEEvaluation', 'UseIncrementalNeuralEvaluation',
    'Hash', 'UsePawnShield', 'UsePawnStructure', 'UseLMR',
)
# python-chess looks options up without case, while Regicide's UCI handlers
# compare these spellings exactly. Never let a profile describe an alias that
# configure() accepts but the native engine ignores. Advertised ranges and
# support for any other option remain the existing match runner's job.
_REGICIDE_OPTIONS = set(REQUIRED_OPTIONS) | {
    'Move Overhead', 'UsePositionalEvaluation', 'UseQuiescence', 'UseTranspositionTable',
    'Clear Hash', 'UseFastMoves', 'UseQuietMoveOrdering', 'UseQuiescenceChecks',
    'PVS', 'UseSEE', 'EvalFile', 'NeuralModelFile', 'UseQuiescenceThreats',
    'UseQuiescenceThreatCache',
    'UseTacticalThreatOrdering',
    'UseQuiescenceReplyBudget',
}
_CANONICAL_OPTIONS = {name.casefold(): name for name in _REGICIDE_OPTIONS}
_METADATA = {'manifest_path', 'manifest_sha256', 'normalized_sha256'}
_SHA = re.compile(r'[0-9a-fA-F]{64}')
_RESERVED = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
             *(f'lpt{i}' for i in range(1, 10))}


class ProfileError(ValueError):
    """A profile or its declared immutable files do not agree."""


def _sha(path):
    try:
        with Path(path).open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()
    except OSError as error:
        raise ProfileError(f'cannot read profile file: {path}') from error


def _string(value, label):
    if not isinstance(value, str) or not value.strip() or any(c in value for c in '\r\n\0'):
        raise ProfileError(f'{label} must be a nonempty single-line string')
    return value


def _path(value, base):
    value = _string(value, 'file path')
    path = Path(value).expanduser()
    return (base / path if not path.is_absolute() else path).resolve()


def _record(value, base, label):
    if not isinstance(value, dict):
        raise ProfileError(f'{label} must be a file object')
    result = deepcopy(value)
    result['path'] = str(_path(value.get('path'), base))
    digest = value.get('sha256')
    if not isinstance(digest, str) or not _SHA.fullmatch(digest):
        raise ProfileError(f'{label}.sha256 must contain 64 hexadecimal characters')
    result['sha256'] = digest.lower()
    if _sha(result['path']) != result['sha256']:
        raise ProfileError(f'{label} SHA256 mismatch: {result["path"]}')
    return result


def _relative_path(value):
    value = _string(value, 'artifact relative_path')
    path = PurePosixPath(value)
    if (path.is_absolute() or any(c in value for c in '\\:<>"|?*') or
            any(ord(c) < 32 for c in value) or
            any(part in ('', '.', '..') for part in value.split('/')) or
            any(part.rstrip(' .') != part or part.split('.')[0].lower() in _RESERVED
                for part in path.parts)):
        raise ProfileError('artifact relative_path must be a safe portable relative path')
    return path.as_posix()


def _fingerprint(profile):
    literal = {key: value for key, value in profile.items() if key not in _METADATA}
    return hashlib.sha256(json.dumps(literal, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def _normalize(profile, base):
    if not isinstance(profile, dict) or type(profile.get('schema_version')) is not int or profile['schema_version'] != 1:
        raise ProfileError('engine profile requires schema_version 1')
    result = {key: deepcopy(value) for key, value in profile.items() if key not in _METADATA}
    identifier = result.get('id')
    if (not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', identifier)
            or identifier.split('.')[0].lower() in _RESERVED or identifier.endswith('.')):
        raise ProfileError('profile id must be a portable slug')
    kind, evaluation = result.get('kind'), result.get('evaluation')
    if kind not in ('regicide', 'external') or evaluation not in ('classical', 'residual', 'nnue', 'external'):
        raise ProfileError('unknown engine kind or evaluation family')
    if (kind == 'external') != (evaluation == 'external'):
        raise ProfileError('external kind and evaluation must be selected together')
    result['binary'] = _record(result.get('binary'), base, 'binary')
    args = result.get('args')
    if not isinstance(args, list) or any(not isinstance(arg, str) or any(c in arg for c in '\r\n\0') for arg in args):
        raise ProfileError('args must be a literal array of single-line strings')
    if kind == 'regicide' and args != ['uci']:
        raise ProfileError('Regicide args must be ["uci"]')
    options = result.get('options')
    if not isinstance(options, dict) or any(not isinstance(key, str) for key in options):
        raise ProfileError('options must be an object')
    folded_options = set()
    for name in options:
        folded = name.casefold()
        if folded in folded_options:
            raise ProfileError(f'case-insensitive duplicate profile option: {name}')
        folded_options.add(folded)
        if kind == 'regicide' and folded in _CANONICAL_OPTIONS and name != _CANONICAL_OPTIONS[folded]:
            raise ProfileError(f'Regicide option must use canonical spelling: {_CANONICAL_OPTIONS[folded]}')
    try:
        result['options'] = parse_options(json.dumps(options, allow_nan=False), 'profile options')
    except (ValueError, TypeError) as error:
        raise ProfileError(str(error)) from error
    if any(isinstance(value, str) and any(c in value for c in '\r\n\0') for value in options.values()):
        raise ProfileError('profile option strings must be single-line')
    source = result.get('source')
    if not isinstance(source, dict):
        raise ProfileError('source must be an object')
    if kind == 'regicide':
        try:
            source['commit'] = verified_commit(_string(source.get('commit'), 'source.commit'))
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            raise ProfileError(f'invalid Regicide source commit: {error}') from error
    else:
        for key in ('name', 'version', 'source_url', 'revision'):
            _string(source.get(key), 'source.' + key)
        url = urlsplit(source['source_url'])
        if url.scheme != 'https' or not url.hostname or url.username is not None or url.password is not None:
            raise ProfileError('external source_url must be HTTPS without credentials')
    models = result.get('models', {})
    if not isinstance(models, dict) or any(key not in ('EvalFile', 'NeuralModelFile') for key in models):
        raise ProfileError('models must map EvalFile or NeuralModelFile to file objects')
    result['models'] = {}
    for key, model in models.items():
        normalized = _record(model, base, 'model ' + key)
        if key not in options or _path(options[key], base) != Path(normalized['path']):
            raise ProfileError(f'{key} option must bind exactly to its frozen model file')
        result['models'][key] = normalized
        result['options'][key] = normalized['path']
        if kind == 'regicide':
            with Path(normalized['path']).open('rb') as stream:
                magic = stream.read(256).split()
            valid = (magic[:1] in ([b'REGICIDE_NN_V1'], [b'REGICIDE_NN_V2']) if key == 'EvalFile'
                     else len(magic) > 1 and magic[0] == b'REGICIDE_NNUE' and magic[1] in (b'1', b'2', b'3'))
            if not valid:
                raise ProfileError(f'{key} model has the wrong Regicide model family or version')
    if kind == 'regicide':
        if any(key not in options for key in REQUIRED_OPTIONS):
            raise ProfileError('Regicide profile must explicitly set ' + ', '.join(REQUIRED_OPTIONS))
        if any(type(options[key]) is not bool for key in REQUIRED_OPTIONS if key != 'Hash') or type(options['Hash']) is not int:
            raise ProfileError('Regicide mode/search flags must be booleans and Hash an integer')
        if 'UseQuiescenceThreats' in options and type(options['UseQuiescenceThreats']) is not bool:
            raise ProfileError('UseQuiescenceThreats must be a boolean')
        if 'UseQuiescenceThreatCache' in options and type(options['UseQuiescenceThreatCache']) is not bool:
            raise ProfileError('UseQuiescenceThreatCache must be a boolean')
        if 'UseTacticalThreatOrdering' in options and type(options['UseTacticalThreatOrdering']) is not bool:
            raise ProfileError('UseTacticalThreatOrdering must be a boolean')
        if 'UseQuiescenceReplyBudget' in options and type(options['UseQuiescenceReplyBudget']) is not bool:
            raise ProfileError('UseQuiescenceReplyBudget must be a boolean')
        if options['UseNeuralEvaluation'] != (evaluation == 'residual') or options['UseNNUEEvaluation'] != (evaluation == 'nnue'):
            raise ProfileError('evaluation family conflicts with neural activation options')
        if evaluation in ('classical', 'residual') and options.get('UsePositionalEvaluation', True) is not True:
            raise ProfileError('classical/residual profiles require positional evaluation')
        required_model = {'residual': 'EvalFile', 'nnue': 'NeuralModelFile'}.get(evaluation)
        if required_model and required_model not in models:
            raise ProfileError(f'{evaluation} requires a frozen {required_model}; builtin NNUE is not an M2 candidate')
        for key, empty in (('EvalFile', '<empty>'), ('NeuralModelFile', '<builtin>')):
            if key in options and key not in models and options[key] not in ('', empty):
                raise ProfileError(f'{key} refers to a model not declared in models')
        if options.get('EvalFile') == '':
            raise ProfileError('use <empty>, not an empty EvalFile string, to clear residual weights')
    if 'artifacts' in result and 'artifacts' in source:
        raise ProfileError('declare artifacts at the top level or under source, not both')
    artifacts = result.get('artifacts', source.pop('artifacts', []))
    if not isinstance(artifacts, list):
        raise ProfileError('artifacts must be a list')
    result['artifacts'] = []
    for index, artifact in enumerate(artifacts):
        entry = _record(artifact, base, f'artifact {index}')
        _string(entry.get('role'), 'artifact role')
        if 'relative_path' in entry:
            entry['relative_path'] = _relative_path(entry['relative_path'])
        result['artifacts'].append(entry)
    bindings = {str(_path(entry['path'], base)): entry['path'] for entry in result['artifacts']}
    for index, arg in enumerate(args):
        if arg and not arg.startswith('-'):
            candidate = str(_path(arg, base))
            if candidate in bindings:
                result['args'][index] = bindings[candidate]
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProfileError(f'duplicate JSON profile key: {key}')
        result[key] = value
    return result


def _reject_constant(_value):
    raise ProfileError('non-finite JSON number')


def load_profile(path):
    """Load and verify a profile; all referenced file paths become absolute."""
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
        literal = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_object,
                             parse_constant=_reject_constant)
        result = _normalize(literal, path.parent)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProfileError(f'cannot read engine profile: {path}') from error
    result.update(manifest_path=str(path), manifest_sha256=hashlib.sha256(raw).hexdigest(),
                  normalized_sha256=_fingerprint(result))
    return result


def verify_profile(profile):
    """Recheck literal options and all hashes, without launching an engine."""
    base = (Path(profile['manifest_path']).resolve().parent
            if isinstance(profile, dict) and profile.get('manifest_path') else Path.cwd())
    normalized = _normalize(profile, base)
    if profile.get('manifest_path') and _sha(profile['manifest_path']) != profile.get('manifest_sha256'):
        raise ProfileError('profile manifest SHA256 mismatch')
    if profile.get('normalized_sha256') and _fingerprint(normalized) != profile['normalized_sha256']:
        raise ProfileError('loaded profile contents changed')


def snapshot_profile(profile, destination_dir):
    """Copy verified files to a new directory; publish profile.json last.

    A failed copy leaves an incomplete directory without a usable profile. The
    destination is never reused or overwritten, and source files are rechecked.
    """
    verify_profile(profile)
    snapshot = {key: deepcopy(value) for key, value in profile.items() if key not in _METADATA}
    destination = Path(destination_dir).expanduser().resolve()
    records = [(snapshot['binary'], 'binary/' + Path(snapshot['binary']['path']).name)]
    records += [(entry, 'models/' + key + '-' + Path(entry['path']).name)
                for key, entry in snapshot['models'].items()]
    records += [(entry, entry.get('relative_path', f'artifacts/{index}-' + Path(entry['path']).name))
                for index, entry in enumerate(snapshot['artifacts'])]
    claimed = ['profile.json']
    for _entry, relative in records:
        relative = _relative_path(relative)
        folded = relative.casefold()
        if any(folded == other or folded.startswith(other + '/') or other.startswith(folded + '/') for other in claimed):
            raise ProfileError('snapshot file destinations collide')
        claimed.append(folded)
    destination.mkdir(parents=True, exist_ok=False)
    replacements = {}
    for entry, relative in records:
        source = Path(entry['path'])
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open('rb') as incoming, target.open('xb') as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        shutil.copymode(source, target)
        if _sha(target) != entry['sha256'] or _sha(source) != entry['sha256']:
            raise ProfileError(f'file changed while snapshotting: {source}')
        replacements[str(source)] = str(target)
        entry['path'] = relative
    for key, entry in snapshot['models'].items():
        snapshot['options'][key] = entry['path']
    snapshot['args'] = [replacements.get(arg, arg) for arg in snapshot['args']]
    snapshot['args'] = [str(Path(arg).relative_to(destination)).replace('\\', '/')
                        if arg in replacements.values() else arg for arg in snapshot['args']]
    verify_profile(profile)
    output = destination / 'profile.json'
    with output.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(snapshot, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    return load_profile(output)


def compare_arguments(profile):
    """Return literal values for the existing runner, which validates UCI itself."""
    verify_profile(profile)
    source = profile['source']
    external = None
    if profile['kind'] == 'external':
        external = {key: source[key] for key in ('name', 'version', 'source_url', 'revision')}
        external.update(binary_sha256=profile['binary']['sha256'], args=list(profile['args']))
    options = profile['options']
    activation = ('UseNeuralEvaluation', 'UseNNUEEvaluation')
    ordered = {key: value for key, value in options.items() if key in activation and value is False}
    ordered.update({key: value for key, value in options.items() if key in profile['models']})
    ordered.update({key: value for key, value in options.items() if key not in activation and key not in profile['models']})
    ordered.update({key: value for key, value in options.items() if key in activation and value is not False})
    return {'binary': profile['binary']['path'], 'args': list(profile['args']), 'options': ordered,
            'commit': source['commit'] if profile['kind'] == 'regicide' else None,
            'external_source': external}
