"""Offline exact endpoint resolution fixtures; no engines/processes are started."""
from copy import deepcopy
import ctypes as C
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import m2_engine_identity as identity


def put(pointer, value):
    pointer._obj.low, pointer._obj.high = value & 0xffffffff, value >> 32


class EngineIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.regicide = self.file('regicide.exe', b'regicide fixture')
        self.stockfish = self.file('stockfish-windows.exe', b'stockfish fixture')
        self.wrapper = self.file('venv/python.exe', b'python venv launcher fixture')
        self.runtime = self.file('base/python.exe', b'base Python fixture')
        self.script = self.file('source/sunfish.py', b'print("UCI fixture")')
        self.helper = self.file('console/conhost.exe', b'frozen Windows console fixture')
        self.script.update(role='engine-source')
        self.native_profile = dict(id='regicide-fixture', kind='regicide', binary=self.regicide,
                                   args=['uci'], source=dict(commit='a'*40), artifacts=[])
        self.sunfish_profile = dict(id='sunfish-fixture', kind='external', binary=self.wrapper,
            args=['-u', self.script['path']], source=dict(name='Sunfish', source_url='https://github.com/thomasahle/sunfish',
                revision='e396948acbd80cdcbad3dccfb07e878fba9df8f4'), artifacts=[self.script])
        self.rows = {10: dict(pid=10, parent_pid=1), 20: dict(pid=20, parent_pid=1),
                     21: dict(pid=21, parent_pid=20)}
        self.images = {10: self.regicide['path'], 20: self.wrapper['path'], 21: self.runtime['path']}
        self.creations = {10: 1000, 20: 2000, 21: 2001}
        self.exits = {10: 0, 20: 0, 21: 0}
        self.api = Mock()
        self.api.OpenProcess.side_effect = lambda _access, _inherit, pid: pid+1000
        self.api.GetProcessId.side_effect = lambda handle: handle-1000
        self.api.GetProcessTimes.side_effect = self.times
        self.api.GetExitCodeProcess.side_effect = self.exitcode
        self.api.WaitForSingleObject.side_effect = lambda handle, _timeout: 0 if self.exits[handle-1000] else 258
        self.api.QueryFullProcessImageNameW.side_effect = self.image
        self.api.CloseHandle.return_value = True
        self.inventory = patch.object(identity, '_inventory', side_effect=lambda _api: deepcopy(self.rows))
        self.inventory_mock = self.inventory.start()
        self.addCleanup(self.inventory.stop)

    def file(self, relative, data):
        path = self.folder/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return dict(path=str(path.resolve()), sha256=hashlib.sha256(data).hexdigest())

    def times(self, handle, creation, exited, kernel, user):
        pid = handle-1000
        put(creation, self.creations[pid])
        put(exited, self.exits[pid])
        put(kernel, 0)
        put(user, 0)
        return True

    def exitcode(self, handle, output):
        output._obj.value = 259 if not self.exits[handle-1000] else 0
        return True

    def image(self, handle, flags, buffer, length):
        self.assertEqual(flags, 0)
        buffer.value = self.images[handle-1000]
        length._obj.value = len(buffer.value)
        return True

    def resolve_native(self, **kwargs):
        return identity.resolve_engine_endpoint(10, 1000, self.native_profile, self.runtime,
                                                 _kernel=self.api, **kwargs)

    def resolve_sunfish(self, **kwargs):
        return identity.resolve_engine_endpoint(20, 2000, self.sunfish_profile, self.runtime,
                                                 _kernel=self.api, **kwargs)

    def test_native_regicide_is_frozen_transport_exact_identity(self):
        result = self.resolve_native()
        self.assertEqual(result['mode'], 'native-direct')
        self.assertEqual(result['transport'], result['endpoint'])
        self.assertEqual(result['endpoint']['pid'], 10)
        self.assertEqual(result['endpoint']['creation_ticks'], 1000)
        self.assertEqual(result['endpoint']['image_sha256'], self.regicide['sha256'])
        self.api.OpenProcess.assert_called_once_with(0x101000, False, 10)
        self.api.CloseHandle.assert_called_once_with(1010)
        self.assertFalse(self.api.TerminateProcess.called)
        self.api.WaitForSingleObject.assert_called_with(1010, 0)
        json.dumps(result, allow_nan=False)

    def test_native_stockfish_scope(self):
        self.native_profile.update(kind='external', binary=self.stockfish, args=[], source=dict(name='Stockfish'))
        self.images[10] = self.stockfish['path']
        self.assertEqual(self.resolve_native()['mode'], 'native-direct')

    def test_sunfish_resolves_unique_frozen_actual_child_not_launcher(self):
        result = self.resolve_sunfish()
        self.assertEqual(result['mode'], 'sunfish-venv-child')
        self.assertEqual(result['transport']['pid'], 20)
        self.assertEqual(result['endpoint']['pid'], 21)
        self.assertEqual(result['endpoint']['parent_pid'], 20)
        self.assertEqual(result['endpoint']['image_path'], self.runtime['path'])
        self.assertEqual(result['endpoint']['image_sha256'], self.runtime['sha256'])
        self.assertEqual(result['frozen_script']['sha256'], self.script['sha256'])
        self.assertEqual(result['observed_direct_children'], [21])
        self.assertEqual(result['matching_inventories'][0], result['matching_inventories'][1])
        self.assertEqual(self.api.OpenProcess.call_args_list[0].args, (0x101000, False, 20))
        self.assertEqual(self.api.OpenProcess.call_args_list[1].args, (0x101000, False, 21))
        self.assertEqual([call.args[0] for call in self.api.CloseHandle.call_args_list], [1021, 1020])

    def test_unknown_profile_or_python_script_not_adopted(self):
        for mutation in ('unknown_source', 'unknown_url', 'unknown_script', 'extra_args'):
            with self.subTest(mutation=mutation):
                original = deepcopy(self.sunfish_profile)
                if mutation == 'unknown_source': self.sunfish_profile['source']['name'] = 'Other Engine'
                if mutation == 'unknown_url': self.sunfish_profile['source']['source_url'] = 'https://example.com/sunfish'
                if mutation == 'unknown_script': self.sunfish_profile['artifacts'] = []
                if mutation == 'extra_args': self.sunfish_profile['args'].append('--other')
                with self.assertRaises(identity.EngineIdentityError): self.resolve_sunfish()
                self.sunfish_profile = original
        self.api.OpenProcess.assert_not_called()

    def test_wrapper_script_and_base_runtime_sha_must_match_frozen_files(self):
        for record in (self.wrapper, self.script, self.runtime):
            with self.subTest(path=record['path']):
                digest = record['sha256']
                record['sha256'] = '0'*64
                with self.assertRaisesRegex(identity.EngineIdentityError, 'SHA256 differs'):
                    self.resolve_sunfish()
                record['sha256'] = digest
        self.api.OpenProcess.assert_not_called()

    def test_native_image_mismatch_and_transport_creation_reuse_fail(self):
        self.images[10] = self.stockfish['path']
        with self.assertRaisesRegex(identity.EngineIdentityError, 'image path differs'): self.resolve_native()
        self.images[10] = self.regicide['path']
        self.creations[10] += 1
        with self.assertRaisesRegex(identity.EngineIdentityError, 'creation identity differs'): self.resolve_native()

    def test_child_image_mismatch_creation_or_parent_invalid_fail(self):
        self.images[21] = self.wrapper['path']
        with self.assertRaisesRegex(identity.EngineIdentityError, 'image path differs'): self.resolve_sunfish()
        self.images[21] = self.runtime['path']
        self.creations[21] = 1999
        with self.assertRaisesRegex(identity.EngineIdentityError, 'predates'): self.resolve_sunfish()
        self.creations[21] = 2001
        self.rows[21]['parent_pid'] = 1
        direct = self.resolve_sunfish()
        self.assertEqual(direct['mode'], 'native-direct')
        self.assertEqual(direct['endpoint']['pid'], 20)

    def test_multiple_children_grandchild_and_unknown_native_child_fail_closed(self):
        old_rows = deepcopy(self.rows)
        for mode in ('multiple', 'grandchild', 'native_child'):
            self.rows = deepcopy(old_rows)
            with self.subTest(mode=mode):
                if mode == 'multiple': self.rows[22] = dict(pid=22, parent_pid=20)
                if mode == 'grandchild': self.rows[22] = dict(pid=22, parent_pid=21)
                if mode == 'native_child': self.rows[22] = dict(pid=22, parent_pid=10)
                self.creations[22], self.exits[22], self.images[22] = 2002, 0, self.runtime['path']
                with self.assertRaises(identity.EngineIdentityError):
                    self.resolve_native() if mode == 'native_child' else self.resolve_sunfish()

    def test_sunfish_child_free_frozen_direct_variant_preserves_script_runtime(self):
        self.rows.pop(21)
        result = self.resolve_sunfish()
        self.assertEqual(result['mode'], 'native-direct')
        self.assertEqual(result['endpoint'], result['transport'])
        self.assertEqual(result['frozen_script']['sha256'], self.script['sha256'])
        self.assertEqual(result['expected_base_runtime'], self.runtime)
        self.assertIn('Sunfish', result['allowed_scope'])

    def add_helper(self, pid=22, parent=20):
        self.rows[pid] = dict(pid=pid, parent_pid=parent)
        self.creations[pid] = self.creations[parent]+pid
        self.exits[pid] = 0
        self.images[pid] = self.helper['path']

    def test_frozen_console_helper_is_optional_and_explicitly_recorded(self):
        self.add_helper()
        with self.assertRaisesRegex(identity.EngineIdentityError, 'image path differs'): self.resolve_sunfish()
        result = self.resolve_sunfish(expected_console_helper=self.helper)
        self.assertEqual(result['mode'], 'sunfish-venv-child')
        self.assertEqual(result['endpoint']['pid'], 21)
        self.assertEqual(result['observed_direct_children'], [21, 22])
        self.assertEqual(result['expected_console_helper'], self.helper)
        self.assertEqual(result['console_helpers'][0]['pid'], 22)
        self.assertEqual(result['console_helpers_after'][0]['creation_ticks'], self.creations[22])
        self.rows.pop(22)
        result = self.resolve_sunfish(expected_console_helper=self.helper)
        self.assertEqual(result['console_helpers'], [])

    def test_native_one_frozen_helper_allowed_but_multiple_helpers_rejected(self):
        self.add_helper(parent=10)
        result = self.resolve_native(expected_console_helper=self.helper)
        self.assertEqual(result['mode'], 'native-direct')
        self.assertEqual(result['endpoint']['pid'], 10)
        self.assertEqual(result['console_helpers'][0]['pid'], 22)
        self.add_helper(pid=23, parent=10)
        with self.assertRaisesRegex(identity.EngineIdentityError, 'multiple console'):
            self.resolve_native(expected_console_helper=self.helper)

    def test_console_helper_hash_name_and_descendants_remain_fail_closed(self):
        self.add_helper()
        bad = dict(self.helper, sha256='0'*64)
        with self.assertRaisesRegex(identity.EngineIdentityError, 'SHA256 differs'):
            self.resolve_sunfish(expected_console_helper=bad)
        with self.assertRaisesRegex(identity.EngineIdentityError, 'conhost.exe'):
            self.resolve_sunfish(expected_console_helper=self.runtime)
        self.rows[23] = dict(pid=23, parent_pid=22)
        with self.assertRaisesRegex(identity.EngineIdentityError, 'descendant'):
            self.resolve_sunfish(expected_console_helper=self.helper)

    def test_inaccessible_endpoint_or_native_query_failure_fails_and_closes(self):
        self.api.OpenProcess.side_effect = lambda _a, _b, pid: 0 if pid == 21 else 1020
        with self.assertRaisesRegex(identity.EngineIdentityError, 'OpenProcess'): self.resolve_sunfish()
        self.api.CloseHandle.assert_called_once_with(1020)
        self.api.OpenProcess.side_effect = lambda _a, _b, pid: pid+1000
        self.api.GetProcessTimes.side_effect = None
        self.api.GetProcessTimes.return_value = False
        with self.assertRaisesRegex(identity.EngineIdentityError, 'GetProcessTimes'): self.resolve_sunfish()

    def test_exited_child_and_even_exit_259_are_rejected(self):
        self.exits[21] = 10000
        with self.assertRaisesRegex(identity.EngineIdentityError, 'exited'): self.resolve_sunfish()
        self.api.GetExitCodeProcess.side_effect = lambda _h, output: setattr(output._obj, 'value', 259) or True
        with self.assertRaisesRegex(identity.EngineIdentityError, 'exited'): self.resolve_sunfish()

    def test_running_undefined_exit_time_is_ignored_and_wait_failure_is_fatal(self):
        original_times = self.api.GetProcessTimes.side_effect
        def arbitrary_exit(handle, creation, exited, kernel, user):
            success = original_times(handle, creation, exited, kernel, user)
            put(exited, 987654321)
            return success
        self.api.GetProcessTimes.side_effect = arbitrary_exit
        result = self.resolve_sunfish()
        self.assertEqual(result['endpoint']['wait_status'], 258)
        self.assertNotIn('exit_time_ticks', result['endpoint'])
        self.api.WaitForSingleObject.side_effect = None
        self.api.WaitForSingleObject.return_value = 0xffffffff
        with self.assertRaises(identity.EngineIdentityError): self.resolve_sunfish()

    def test_tree_change_between_two_inventories_fails_closed(self):
        second = deepcopy(self.rows)
        second[21]['parent_pid'] = 1
        self.inventory_mock.side_effect = [deepcopy(self.rows), second]
        with self.assertRaisesRegex(identity.EngineIdentityError, 'inventory changed'): self.resolve_sunfish()

    def test_child_creation_change_after_inventory_fails_even_with_held_handle(self):
        def inventories(_api):
            if self.inventory_mock.call_count == 2:
                self.creations[21] += 1
            return deepcopy(self.rows)
        self.inventory_mock.side_effect = inventories
        with self.assertRaisesRegex(identity.EngineIdentityError, 'creation identity differs'): self.resolve_sunfish()

    def test_close_failure_attempts_to_close_every_owned_handle(self):
        self.api.CloseHandle.side_effect = [False, True]
        with self.assertRaisesRegex(identity.EngineIdentityError, 'CloseHandle'): self.resolve_sunfish()
        self.assertEqual([call.args[0] for call in self.api.CloseHandle.call_args_list], [1021, 1020])

    def test_invalid_pid_creation_and_relative_frozen_path_fail(self):
        for pid, creation in ((True, 1000), (0, 1000), (10, True), (10, 0)):
            with self.assertRaises(identity.EngineIdentityError):
                identity.resolve_engine_endpoint(pid, creation, self.native_profile, self.runtime, _kernel=self.api)
        self.native_profile['binary'] = dict(self.regicide, path='regicide.exe')
        with self.assertRaisesRegex(identity.EngineIdentityError, 'absolute frozen path'): self.resolve_native()

    def test_inventory_adapter_requires_completed_native_enumeration(self):
        self.inventory.stop()
        self.api.CreateToolhelp32Snapshot.return_value = 9000
        def first(_handle, entry):
            entry._obj.pid, entry._obj.parent_pid = 10, 1
            return True
        self.api.Process32FirstW.side_effect = first
        self.api.Process32NextW.return_value = False
        with patch.object(identity, '_last_error', return_value=18):
            self.assertEqual(identity._inventory(self.api), {10: dict(pid=10, parent_pid=1)})
        with patch.object(identity, '_last_error', return_value=5):
            with self.assertRaisesRegex(identity.EngineIdentityError, 'did not complete'):
                identity._inventory(self.api)
        self.assertEqual(self.api.CloseHandle.call_count, 2)


if __name__ == '__main__':
    unittest.main()
