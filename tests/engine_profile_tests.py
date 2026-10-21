"""Immutable engine/profile files, mode separation and portable source snapshots."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import engine_profile as profiles


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EngineProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                    capture_output=True, text=True, check=True).stdout.strip()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='regicide-engine-profile-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.binary = self.folder / 'engine.exe'
        self.binary.write_bytes(b'profile binary fixture; never launched')
        self.residual = self.folder / 'residual model.nn'
        self.residual.write_bytes(b'REGICIDE_NN_V2 768 32 16 600 100\nfixture\n')
        self.nnue = self.folder / 'integer  model.nnue'
        self.nnue.write_bytes(b'REGICIDE_NNUE 2 768 16 4064 256 32\nfixture\n')
        self.receipt = self.folder / 'build-receipt.json'
        self.receipt.write_text('{"scope":"fixture, no build-source proof"}\n', encoding='utf-8')
        self.path = self.folder / 'input-profile.json'

    def record(self, path):
        return {'path': path.name, 'sha256': digest(path)}

    def fixture(self, family='classical'):
        options = {'UseNeuralEvaluation': family == 'residual', 'UseNNUEEvaluation': family == 'nnue',
                   'UseIncrementalNeuralEvaluation': True, 'Hash': 16, 'UsePawnShield': True,
                   'UsePawnStructure': True, 'UseLMR': True, 'UsePositionalEvaluation': True}
        models = {}
        if family in ('residual', 'nnue'):
            key, file = ('EvalFile', self.residual) if family == 'residual' else ('NeuralModelFile', self.nnue)
            models[key] = self.record(file)
            options[key] = file.name
        return {'schema_version': 1, 'id': family + '-fixture', 'kind': 'regicide',
                'evaluation': family, 'binary': self.record(self.binary), 'args': ['uci'],
                'options': options, 'source': {'commit': self.commit}, 'models': models,
                'artifacts': [self.record(self.receipt) | {'role': 'build-receipt'}]}

    def load(self, literal=None):
        self.path.write_text(json.dumps(literal or self.fixture(), ensure_ascii=False), encoding='utf-8')
        return profiles.load_profile(self.path)

    def rejects(self, literal):
        self.path.write_text(json.dumps(literal), encoding='utf-8')
        with self.assertRaises(profiles.ProfileError):
            profiles.load_profile(self.path)

    def test_all_three_families_bind_paths_and_real_commit_without_engine_start(self):
        for family in ('classical', 'residual', 'nnue'):
            with self.subTest(family=family):
                loaded = self.load(self.fixture(family))
                profiles.verify_profile(loaded)
                self.assertEqual(loaded['binary']['path'], str(self.binary))
                self.assertEqual(loaded['source']['commit'], self.commit)
                for key, model in loaded['models'].items():
                    self.assertTrue(Path(model['path']).is_absolute())
                    self.assertEqual(loaded['options'][key], model['path'])
                arguments = profiles.compare_arguments(loaded)
                self.assertEqual(arguments['commit'], self.commit)
                self.assertEqual(arguments['args'], ['uci'])
                self.assertIsNone(arguments['external_source'])

    def test_contradictory_activation_flags_and_missing_literal_options_rejected(self):
        for family in ('classical', 'residual', 'nnue'):
            for key in ('UseNeuralEvaluation', 'UseNNUEEvaluation'):
                literal = self.fixture(family)
                literal['options'][key] = not literal['options'][key]
                self.rejects(literal)
        for key in profiles.REQUIRED_OPTIONS:
            literal = self.fixture()
            del literal['options'][key]
            self.rejects(literal)
        for key, value in (('Hash', True), ('UseLMR', 1), ('UsePositionalEvaluation', False)):
            literal = self.fixture()
            literal['options'][key] = value
            self.rejects(literal)

    def test_v3_nnue_and_explicit_tactical_search_option(self):
        self.nnue.write_bytes(b'REGICIDE_NNUE 3 768 16 4064 256 32\nfixture\n')
        literal = self.fixture('nnue')
        literal['options']['UseQuiescenceThreats'] = True
        loaded = self.load(literal)
        self.assertIs(loaded['options']['UseQuiescenceThreats'], True)
        literal['options']['UseQuiescenceThreats'] = 1
        self.rejects(literal)
        del literal['options']['UseQuiescenceThreats']
        literal['options']['usequiescencethreats'] = True
        self.rejects(literal)

    def test_quiescence_cache_option_requires_exact_name_and_boolean(self):
        literal = self.fixture('nnue')
        literal['options']['UseQuiescenceThreatCache'] = True
        self.assertIs(self.load(literal)['options']['UseQuiescenceThreatCache'], True)
        literal['options']['UseQuiescenceThreatCache'] = 1
        self.rejects(literal)
        del literal['options']['UseQuiescenceThreatCache']
        literal['options']['usequiescencethreatcache'] = True
        self.rejects(literal)

    def test_reply_budget_option_requires_exact_name_and_boolean(self):
        literal = self.fixture('nnue')
        literal['options']['UseQuiescenceReplyBudget'] = True
        self.assertIs(self.load(literal)['options']['UseQuiescenceReplyBudget'], True)
        literal['options']['UseQuiescenceReplyBudget'] = 1
        self.rejects(literal)
        del literal['options']['UseQuiescenceReplyBudget']
        literal['options']['usequiescencereplybudget'] = True
        self.rejects(literal)

    def test_model_family_binding_and_builtin_are_rejected(self):
        literal = self.fixture('nnue')
        literal['options']['UseTacticalThreatOrdering'] = True
        self.assertIs(self.load(literal)['options']['UseTacticalThreatOrdering'], True)
        literal['options']['UseTacticalThreatOrdering'] = 1
        self.rejects(literal)
        literal = self.fixture('nnue')
        literal['models'] = {}
        literal['options']['NeuralModelFile'] = '<builtin>'
        self.rejects(literal)
        for family, key, wrong in (('nnue', 'NeuralModelFile', self.residual),
                                   ('residual', 'EvalFile', self.nnue)):
            literal = self.fixture(family)
            literal['models'][key] = self.record(wrong)
            literal['options'][key] = wrong.name
            self.rejects(literal)
        literal = self.fixture('residual')
        literal['options']['EvalFile'] = self.nnue.name
        self.rejects(literal)
        literal = self.fixture()
        literal['options']['EvalFile'] = self.residual.name
        self.rejects(literal)
        literal['options']['EvalFile'] = ''
        self.rejects(literal)

    def test_casefold_duplicate_options_are_rejected_for_both_kinds(self):
        for family in ('classical', 'residual', 'nnue'):
            for name, value in (('hash', 32), ('usepositionalevaluation', False),
                                ('useneuralevaluation', True)):
                literal = self.fixture(family)
                literal['options'][name] = value
                self.rejects(literal)
        literal = self.external_fixture()
        literal['options'] = {'Hash': 16, 'hash': 32}
        self.rejects(literal)

    def test_single_noncanonical_regicide_aliases_cannot_bypass_mode_or_model_checks(self):
        aliases = {
            'Move Overhead': 'move overhead', 'UsePositionalEvaluation': 'usepositionalevaluation',
            'UseQuiescence': 'usequiescence', 'UseTranspositionTable': 'usetranspositiontable',
            'Hash': 'hash', 'Clear Hash': 'clear hash', 'UseFastMoves': 'usefastmoves',
            'UseQuietMoveOrdering': 'usequietmoveordering', 'UseQuiescenceChecks': 'usequiescencechecks',
            'PVS': 'pvs', 'UseSEE': 'usesee', 'UsePawnShield': 'usepawnshield',
            'UsePawnStructure': 'usepawnstructure', 'UseLMR': 'uselmr', 'EvalFile': 'evalfile',
            'UseNeuralEvaluation': 'useneuralevaluation', 'UseNNUEEvaluation': 'usennueevaluation',
            'NeuralModelFile': 'neuralmodelfile', 'UseIncrementalNeuralEvaluation': 'useincrementalneuralevaluation',
        }
        for canonical, alias in aliases.items():
            literal = self.fixture()
            value = literal['options'].pop(canonical, True)
            literal['options'][alias] = value
            self.path.write_text(json.dumps(literal), encoding='utf-8')
            with self.subTest(alias=alias), self.assertRaisesRegex(profiles.ProfileError, 'canonical spelling'):
                profiles.load_profile(self.path)
        for alias, value in (('evalfile', self.residual.name), ('neuralmodelfile', self.nnue.name),
                              ('useneuralevaluation', True), ('usennueevaluation', True)):
            literal = self.fixture()
            # Remove the canonical mode key too: a lone wrong-case key must be
            # rejected as an alias, rather than relying on required-key checks.
            for canonical in list(literal['options']):
                if canonical.casefold() == alias:
                    del literal['options'][canonical]
            literal['options'][alias] = value
            self.rejects(literal)
        # External engines can legitimately advertise another spelling; the
        # runner validates it. Only duplicate aliases are universally invalid.
        literal = self.external_fixture()
        literal['options'] = {'hash': 16}
        self.assertEqual(self.load(literal)['options'], {'hash': 16})

    def test_wrong_hash_missing_file_and_noncommit_objects_rejected(self):
        for area in ('binary', 'model', 'artifact'):
            literal = self.fixture('nnue')
            item = (literal['binary'] if area == 'binary' else literal['models']['NeuralModelFile']
                    if area == 'model' else literal['artifacts'][0])
            item['sha256'] = '0' * 64
            self.rejects(literal)
        literal = self.fixture()
        literal['binary']['path'] = 'missing.exe'
        self.rejects(literal)
        for commit in ('HEAD', self.commit[:8], '0' * 40):
            literal = self.fixture()
            literal['source']['commit'] = commit
            self.rejects(literal)

    def test_verify_detects_changed_binary_model_artifact_manifest_and_options(self):
        for changed in (self.binary, self.nnue, self.receipt, self.path):
            with self.subTest(path=changed.name):
                loaded = self.load(self.fixture('nnue'))
                before = changed.read_bytes()
                changed.write_bytes(before + b'changed')
                with self.assertRaises(profiles.ProfileError):
                    profiles.verify_profile(loaded)
                changed.write_bytes(before)
        loaded = self.load()
        loaded['options']['Hash'] = 32
        with self.assertRaisesRegex(profiles.ProfileError, 'contents changed'):
            profiles.verify_profile(loaded)

    def test_snapshot_survives_original_changes_and_rebinds_model_options(self):
        loaded = self.load(self.fixture('nnue'))
        output = self.folder / 'frozen'
        frozen = profiles.snapshot_profile(loaded, output)
        self.assertTrue(Path(frozen['binary']['path']).is_relative_to(output))
        model = frozen['models']['NeuralModelFile']
        self.assertEqual(frozen['options']['NeuralModelFile'], model['path'])
        self.assertEqual(Path(model['path']).read_bytes(), self.nnue.read_bytes())
        self.binary.write_bytes(b'original binary subsequently changed')
        self.nnue.unlink()
        self.receipt.unlink()
        self.path.unlink()
        profiles.verify_profile(frozen)
        self.assertEqual(frozen['binary']['sha256'], loaded['binary']['sha256'])
        literal = json.loads((output / 'profile.json').read_text(encoding='utf-8'))
        self.assertFalse(Path(literal['binary']['path']).is_absolute())
        self.assertFalse(Path(literal['models']['NeuralModelFile']['path']).is_absolute())
        self.assertNotIn('manifest_path', literal)

    def external_fixture(self):
        script = self.folder / 'engine with spaces.py'
        helper = self.folder / 'helper.py'
        config = self.folder / 'pyvenv.cfg'
        for path in (script, helper, config):
            path.write_text('# fixture source\n', encoding='utf-8')
        return {'schema_version': 1, 'id': 'external-fixture', 'kind': 'external', 'evaluation': 'external',
                'binary': self.record(self.binary), 'args': ['-u', script.name, 'uci'], 'options': {},
                'source': {'name': 'Fixture', 'version': '1', 'source_url': 'https://example.org/source',
                           'revision': 'declared-not-a-local-git-object'}, 'models': {},
                'artifacts': [self.record(script) | {'role': 'engine-source', 'relative_path': 'artifacts/engine/main.py'},
                              self.record(helper) | {'role': 'engine-source', 'relative_path': 'artifacts/engine/tools/helper.py'},
                              self.record(config) | {'role': 'interpreter-config', 'relative_path': 'binary/pyvenv.cfg'}]}

    def test_external_source_tree_and_argv_relocate_with_the_snapshot(self):
        loaded = self.load(self.external_fixture())
        self.assertTrue(Path(loaded['args'][1]).is_absolute())
        original_source = Path(loaded['args'][1])
        output = self.folder / 'external-frozen'
        frozen = profiles.snapshot_profile(loaded, output)
        self.assertEqual(Path(frozen['args'][1]), output / 'artifacts/engine/main.py')
        self.assertTrue((output / 'artifacts/engine/tools/helper.py').is_file())
        self.assertTrue((output / 'binary/pyvenv.cfg').is_file())
        original_source.unlink()
        profiles.verify_profile(frozen)
        relocated = self.folder / 'relocated'
        shutil.copytree(output, relocated)
        moved = profiles.load_profile(relocated / 'profile.json')
        self.assertEqual(Path(moved['args'][1]), relocated / 'artifacts/engine/main.py')
        arguments = profiles.compare_arguments(moved)
        self.assertIsNone(arguments['commit'])
        self.assertEqual(arguments['external_source']['revision'], 'declared-not-a-local-git-object')
        self.assertEqual(arguments['external_source']['args'], arguments['args'])
        self.assertNotIn('source_commit_object_verified', arguments['external_source'])

    def test_snapshot_rejects_existing_directory_and_destination_collisions(self):
        loaded = self.load()
        existing = self.folder / 'already-there'
        existing.mkdir()
        protected = existing / 'keep.txt'
        protected.write_text('unchanged', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            profiles.snapshot_profile(loaded, existing)
        self.assertEqual(protected.read_text(), 'unchanged')
        for destination in ('profile.json', 'binary/engine.exe', 'binary/ENGINE.EXE', 'binary/engine.exe/child'):
            literal = self.fixture()
            literal['artifacts'][0]['relative_path'] = destination
            loaded = self.load(literal)
            with self.assertRaisesRegex(profiles.ProfileError, 'collide'):
                profiles.snapshot_profile(loaded, self.folder / 'collision')
            self.assertFalse((self.folder / 'collision').exists())

    def test_artifact_path_traversal_and_portability_hazards_rejected(self):
        for relative in ('../outside', '/absolute', 'C:/absolute', 'a\\b', 'a//b', './a',
                         'a/../b', 'a/CON.txt', 'a/trailing.', 'a/trailing ', 'a/b?.py', 'a/b\t.py'):
            literal = self.fixture()
            literal['artifacts'][0]['relative_path'] = relative
            self.rejects(literal)
        literal = self.fixture()
        literal['artifacts'].append(deepcopy(literal['artifacts'][0]))
        literal['artifacts'][0]['relative_path'] = 'same/path'
        literal['artifacts'][1]['relative_path'] = 'SAME/path'
        loaded = self.load(literal)
        with self.assertRaises(profiles.ProfileError):
            profiles.snapshot_profile(loaded, self.folder / 'case-collision')

    def test_nested_artifacts_compatibility_never_silently_discards_files(self):
        literal = self.fixture()
        literal['source']['artifacts'] = literal.pop('artifacts')
        loaded = self.load(literal)
        self.assertEqual(len(loaded['artifacts']), 1)
        self.assertNotIn('artifacts', loaded['source'])
        profiles.verify_profile(loaded)
        literal['artifacts'] = []
        self.rejects(literal)

    def test_source_change_during_copy_does_not_publish_profile(self):
        loaded = self.load()
        original_copy = shutil.copyfileobj
        def mutate_after_copy(incoming, outgoing, *args):
            original_copy(incoming, outgoing, *args)
            if Path(incoming.name) == self.binary:
                self.binary.write_bytes(b'changed while snapshotting')
        with patch.object(profiles.shutil, 'copyfileobj', side_effect=mutate_after_copy):
            with self.assertRaises(profiles.ProfileError):
                profiles.snapshot_profile(loaded, self.folder / 'interrupted')
        self.assertFalse((self.folder / 'interrupted/profile.json').exists())

    def test_model_paths_are_configured_before_activation(self):
        for family, key, active in (('residual', 'EvalFile', 'UseNeuralEvaluation'),
                                    ('nnue', 'NeuralModelFile', 'UseNNUEEvaluation')):
            loaded = self.load(self.fixture(family))
            options = profiles.compare_arguments(loaded)['options']
            self.assertLess(list(options).index(key), list(options).index(active))
            self.assertTrue(options[active])

    def test_malformed_literal_json_and_external_identity_rejected(self):
        for content in ('[]', 'null', '{"schema_version":1,"schema_version":1}', '{"value":NaN}'):
            self.path.write_text(content, encoding='utf-8')
            with self.assertRaises(profiles.ProfileError):
                profiles.load_profile(self.path)
        for key, value in (('source_url', 'http://example.org'), ('source_url', 'https://u:p@example.org'),
                           ('revision', ''), ('name', 'line\nbreak')):
            literal = self.external_fixture()
            literal['source'][key] = value
            self.rejects(literal)
        literal = self.fixture()
        literal['options']['EvalFile'] = 'line\nbreak'
        self.rejects(literal)


if __name__ == '__main__':
    unittest.main()
