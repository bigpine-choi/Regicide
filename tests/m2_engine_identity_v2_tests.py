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
import m2_engine_identity_v2 as identity
from types import SimpleNamespace


def put(pointer, value):
    pointer._obj.low, pointer._obj.high = value & 0xffffffff, value >> 32


def build_scope_fixture(minimal_resolution, worker_pid=900, coordinator_pid=800):
    """Return an enriched copy with explicitly synthetic, zero-time Job proof."""
    value=deepcopy(minimal_resolution)
    value.update(schema_version=1,method=identity.METHOD,read_started_monotonic=0.,read_finished_monotonic=0.)
    transport=value['transport']; endpoint=value['endpoint']
    def enrich(row,parent):
        row.update(parent_pid=parent,wait_status=258,read_started_monotonic=0.,read_finished_monotonic=0.)
    enrich(transport,worker_pid)
    enrich(endpoint,worker_pid if endpoint['pid']==transport['pid'] else transport['pid'])
    if endpoint['pid']==transport['pid']: endpoint.update(transport)
    helpers=value.setdefault('console_helpers',[])
    for helper in helpers: enrich(helper,transport['pid'])
    value['transport_after']=deepcopy(transport)
    value['endpoint_after']=deepcopy(endpoint)
    value['console_helpers_after']=deepcopy(helpers)
    local={row['pid']:row for row in [transport,endpoint,*helpers]}
    value['observed_direct_children']=sorted(set(local)-{transport['pid']})
    inventory=[{'pid':worker_pid,'parent_pid':coordinator_pid}]+[
        {'pid':pid,'parent_pid':row['parent_pid']} for pid,row in sorted(local.items())]
    pids=sorted([worker_pid,*local])
    worker_creation=max(1,min(row['creation_ticks'] for row in local.values())-1)
    def birth(pid,creation,parent=None,parent_creation=None):
        row={'pid':pid,'creation_ticks':creation,'wait_status':258,'in_any_job':True,
             'read_started_monotonic':0.,'read_finished_monotonic':0.}
        if parent is not None: row.update(parent_pid=parent,parent_creation_ticks=parent_creation)
        return row
    worker=birth(worker_pid,worker_creation)
    candidates=[birth(pid,row['creation_ticks'],row['parent_pid'],
                      worker_creation if pid==transport['pid'] else transport['creation_ticks'])
                for pid,row in sorted(local.items())]
    pointer=C.sizeof(C.c_size_t)
    snapshot={'query_job_handle':'NULL-current-immediate-job','worker_pid':worker_pid,
        'coordinator_pid':coordinator_pid,'caller_in_job':True,'limit_flags':0x2000,
        'limits_returned_bytes':C.sizeof(identity._ExtendedLimits),'assigned_processes':len(pids),
        'listed_processes':len(pids),'returned_bytes':8+pointer*len(pids),'buffer_capacity':64,
        'pointer_bytes':pointer,'pids':pids,'query_attempts':[],
        'read_started_monotonic':0.,'read_finished_monotonic':0.}
    snapshot['query_attempts']=[{'success':True,'windows_error':0,'buffer_capacity':64,
        'buffer_bytes':8+64*pointer,'returned_bytes':snapshot['returned_bytes'],
        'assigned_processes':len(pids),'listed_processes':len(pids),'pids':pids}]
    value['matching_inventories']=[{row['pid']:deepcopy(row) for row in inventory if row['pid'] in local} for _ in range(2)]
    value['endpoint_job_scope']={'schema_version':1,'method':identity.SCOPE_METHOD,'worker_pid':worker_pid,
        'coordinator_pid':coordinator_pid,'worker_identity':worker,'worker_identity_after':deepcopy(worker),
        'job_snapshots':[deepcopy(snapshot),deepcopy(snapshot)],
        'global_inventories':[deepcopy(inventory),deepcopy(inventory)],
        'membership_candidates':candidates,'membership_candidates_after':deepcopy(candidates),
        'outside_job_edges':[],'stale_job_edges':[],'local_subtree_pids':sorted(local)}
    return value


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
        self.rows = {42: dict(pid=42, parent_pid=1), 10: dict(pid=10, parent_pid=42), 20: dict(pid=20, parent_pid=42),
                     21: dict(pid=21, parent_pid=20)}
        self.images = {10: self.regicide['path'], 20: self.wrapper['path'], 21: self.runtime['path']}
        self.creations = {42: 900, 10: 1000, 20: 2000, 21: 2001}
        self.exits = {42: 0, 10: 0, 20: 0, 21: 0}
        self.api = Mock()
        self.api.OpenProcess.side_effect = lambda _access, _inherit, pid: pid+1000
        self.api.GetProcessId.side_effect = lambda handle: handle-1000
        self.api.GetProcessTimes.side_effect = self.times
        self.api.GetExitCodeProcess.side_effect = self.exitcode
        self.api.WaitForSingleObject.side_effect = lambda handle, _timeout: 0 if self.exits[handle-1000] else 258
        self.api.QueryFullProcessImageNameW.side_effect = self.image
        self.api.CloseHandle.return_value = True
        self.flags = 0x2000
        self.outside = set()
        self.api.GetCurrentProcess.return_value = 1042
        self.api.IsProcessInJob.side_effect = lambda _handle, _job, output: setattr(output._obj, 'value', 1) or True
        self.api.QueryInformationJobObject.side_effect = self.job_query
        self.os_patch = patch.object(identity, 'os', SimpleNamespace(getpid=lambda:42, getppid=lambda:1))
        self.os_patch.start()
        self.addCleanup(self.os_patch.stop)
        self.inventory = patch.object(identity, '_inventory', side_effect=lambda _api: deepcopy(self.rows))
        self.inventory_mock = self.inventory.start()
        self.addCleanup(self.inventory.stop)

    def job_query(self, handle, kind, buffer, size, returned):
        self.assertIsNone(handle)
        if kind == 9:
            buffer._obj.basic.flags = self.flags
            returned._obj.value = size
            return True
        self.assertEqual(kind,3)
        members=sorted(set(self.rows)-self.outside)
        capacity=(size-8)//C.sizeof(C.c_size_t)
        header=(C.c_uint32*2).from_buffer(buffer)
        header[0],header[1]=len(members),min(len(members),capacity)
        values=(C.c_size_t*header[1]).from_buffer(buffer,8)
        for index,pid in enumerate(members[:capacity]): values[index]=pid
        returned._obj.value=8+header[1]*C.sizeof(C.c_size_t)
        return True

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
        result=self.resolve_sunfish()
        self.assertEqual(result['mode'],'native-direct')
        self.assertEqual(result['endpoint_job_scope']['stale_job_edges'][0]['pid'],21)
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
        self.assertIn('current-Job', result['allowed_scope'])

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
        self.creations[23], self.exits[23], self.images[23] = 3000, 0, self.runtime['path']
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
        with patch.object(identity.previous, '_last_error', return_value=18):
            self.assertEqual(identity._inventory(self.api), {10: dict(pid=10, parent_pid=1)})
        with patch.object(identity.previous, '_last_error', return_value=5):
            with self.assertRaisesRegex(identity.EngineIdentityError, 'did not complete'):
                identity._inventory(self.api)
        self.assertEqual(self.api.CloseHandle.call_count, 2)

    def test_outside_job_protected_old_parent_matches_are_recorded_without_open(self):
        for pid in (1396,1612):
            self.rows[pid]=dict(pid=pid,parent_pid=10)
            self.outside.add(pid)
        original=self.api.OpenProcess.side_effect
        self.api.OpenProcess.side_effect=lambda access,inherit,pid: 0 if pid in (1396,1612) else original(access,inherit,pid)
        result=self.resolve_native()
        self.assertEqual([edge['pid'] for edge in result['endpoint_job_scope']['outside_job_edges']],[1396,1612])
        self.assertEqual([call.args[2] for call in self.api.OpenProcess.call_args_list],[10])
        audit=identity.validate_resolution_scope(result,expected_worker_pid=42)
        self.assertEqual(audit['excluded_outside_job_edges'],2)

    def test_same_job_older_child_excluded_before_image_query_with_positive_proof(self):
        self.rows[99]=dict(pid=99,parent_pid=10)
        self.creations[99],self.exits[99]=800,0
        result=self.resolve_native()
        edge=result['endpoint_job_scope']['stale_job_edges'][0]
        self.assertEqual(edge['pid'],99)
        self.assertLess(edge['child_identity']['creation_ticks'],edge['parent_creation_ticks'])
        self.assertEqual([call.args[0] for call in self.api.QueryFullProcessImageNameW.call_args_list],[1010,1010])
        self.assertEqual(identity.validate_resolution_scope(result)['excluded_stale_job_edges'],1)

    def test_stale_grandchild_requires_creation_proof_but_actual_new_grandchild_fails(self):
        self.rows[23]=dict(pid=23,parent_pid=21)
        self.creations[23],self.exits[23]=1900,0
        result=self.resolve_sunfish()
        self.assertEqual(result['endpoint']['pid'],21)
        self.assertEqual(result['endpoint_job_scope']['stale_job_edges'][0]['pid'],23)
        self.creations[23]=2002
        with self.assertRaisesRegex(identity.EngineIdentityError,'descendant') as caught:self.resolve_sunfish()
        self.assertEqual(caught.exception.identity_receipt['offending_pid'],23)

    def test_actual_job_child_access_failure_still_fatal_and_serializes_pid_scope(self):
        self.rows[23]=dict(pid=23,parent_pid=10)
        self.api.OpenProcess.side_effect=lambda access,inherit,pid: 0 if pid==23 else pid+1000
        with self.assertRaisesRegex(identity.EngineIdentityError,'PID 23') as caught:self.resolve_native()
        receipt=caught.exception.identity_receipt
        self.assertEqual(receipt['offending_pid'],23)
        self.assertEqual(len(receipt['endpoint_job_scope']['global_inventories']),1)
        self.assertIn(23,receipt['endpoint_job_scope']['job_snapshots'][0]['pids'])
        self.assertEqual({call.args[0] for call in self.api.OpenProcess.call_args_list},{0x101000})
        json.dumps(receipt,allow_nan=False)

    def test_missing_worker_transport_or_coordinator_in_job_fails_before_endpoint_adoption(self):
        for variant in ('worker','transport','coordinator'):
            with self.subTest(variant=variant):
                self.outside=set()
                self.rows.pop(1,None)
                if variant=='worker':self.outside.add(42)
                if variant=='transport':self.outside.add(10)
                if variant=='coordinator':self.rows[1]=dict(pid=1,parent_pid=0)
                with self.assertRaises(identity.EngineIdentityError):self.resolve_native()
        self.api.OpenProcess.assert_not_called()

    def test_breakaway_flags_and_missing_current_job_fail_closed(self):
        for flag in (0x800,0x1000,0x1800):
            with self.subTest(flag=flag):
                self.flags=flag
                with self.assertRaisesRegex(identity.EngineIdentityError,'breakaway') as caught:self.resolve_native()
                self.assertEqual(caught.exception.identity_receipt['endpoint_job_scope']['job_snapshots'][0]['limit_flags'],flag)
        self.flags=0x2000
        self.api.IsProcessInJob.side_effect=lambda handle,job,output:setattr(output._obj,'value',0) or True
        with self.assertRaisesRegex(identity.EngineIdentityError,'no verified Job'):self.resolve_native()
        self.api.OpenProcess.assert_not_called()

    def test_pidlist_duplicate_truncated_oversized_or_malformed_counts_are_fatal(self):
        for variant in ('duplicate','truncated','oversized_bytes','bad_count','too_many'):
            with self.subTest(variant=variant):
                def query(handle,kind,buffer,size,returned):
                    result=self.job_query(handle,kind,buffer,size,returned)
                    if kind==3:
                        header=(C.c_uint32*2).from_buffer(buffer)
                        if variant=='duplicate':
                            pids=(C.c_size_t*header[1]).from_buffer(buffer,8);pids[1]=pids[0]
                        if variant=='truncated':returned._obj.value=1
                        if variant=='oversized_bytes':returned._obj.value=size+1
                        if variant=='bad_count':header[1]=header[0]+1
                        if variant=='too_many':header[0]=5000
                    return result
                self.api.QueryInformationJobObject.side_effect=query
                with self.assertRaises(identity.EngineIdentityError):self.resolve_native()

    def test_pidlist_partial_success_grows_buffer_and_replays_complete_attempts(self):
        for pid in range(100,170):self.rows[pid]=dict(pid=pid,parent_pid=700)
        result=self.resolve_native()
        attempts=result['endpoint_job_scope']['job_snapshots'][0]['query_attempts']
        self.assertEqual(len(attempts),2)
        self.assertGreater(attempts[1]['buffer_capacity'],attempts[0]['buffer_capacity'])
        self.assertEqual(identity.validate_resolution_scope(result)['status'],'PASS')

    def test_pidlist_never_complete_is_bounded_and_native_failures_are_fatal(self):
        def query(handle,kind,buffer,size,returned):
            result=self.job_query(handle,kind,buffer,size,returned)
            if kind==3:
                header=(C.c_uint32*2).from_buffer(buffer)
                header[0]=(size-8)//C.sizeof(C.c_size_t)+1
            return result
        self.api.QueryInformationJobObject.side_effect=query
        with self.assertRaises(identity.EngineIdentityError) as caught:self.resolve_native()
        attempts=caught.exception.identity_receipt['endpoint_job_scope']['job_snapshots'][0]['query_attempts']
        self.assertLessEqual(len(attempts),8)
        self.api.QueryInformationJobObject.side_effect=lambda *args:False
        with self.assertRaisesRegex(identity.EngineIdentityError,'QueryInformationJobObject'):self.resolve_native()

    def test_other_lane_job_changes_allowed_but_local_membership_change_fails(self):
        def inventory(_api):
            if self.inventory_mock.call_count==2:self.rows[99]=dict(pid=99,parent_pid=700)
            return deepcopy(self.rows)
        self.inventory_mock.side_effect=inventory
        result=self.resolve_native()
        self.assertNotEqual(result['endpoint_job_scope']['job_snapshots'][0]['pids'],result['endpoint_job_scope']['job_snapshots'][1]['pids'])
        self.assertEqual(identity.validate_resolution_scope(result)['status'],'PASS')
        self.inventory_mock.reset_mock()
        def changed(_api):
            if self.inventory_mock.call_count==2:self.outside.add(10)
            return deepcopy(self.rows)
        self.inventory_mock.side_effect=changed
        with self.assertRaises(identity.EngineIdentityError):self.resolve_native()

    def test_new_actual_job_child_between_inventories_is_not_grandfathered(self):
        def changed(_api):
            if self.inventory_mock.call_count==2:self.rows[99]=dict(pid=99,parent_pid=10)
            return deepcopy(self.rows)
        self.inventory_mock.side_effect=changed
        with self.assertRaises(identity.EngineIdentityError):self.resolve_native()

    def test_pure_scope_fixture_json_roundtrip_and_forged_membership_rejected(self):
        minimal={'mode':'native-direct','transport':dict(pid=10,creation_ticks=1000,image_path=self.regicide['path'],image_sha256=self.regicide['sha256']),
                 'endpoint':dict(pid=10,creation_ticks=1000,image_path=self.regicide['path'],image_sha256=self.regicide['sha256'])}
        enriched=build_scope_fixture(minimal,worker_pid=42,coordinator_pid=1)
        self.assertEqual(identity.validate_resolution_scope(json.loads(json.dumps(enriched)),expected_worker_pid=42)['status'],'PASS')
        for variant in ('breakaway','no_query_attempts','wrong_worker','missing_pid','creation'):
            value=deepcopy(enriched)
            scope=value['endpoint_job_scope']
            if variant=='breakaway':scope['job_snapshots'][0]['limit_flags']=0x800
            if variant=='no_query_attempts':scope['job_snapshots'][0]['query_attempts']=[]
            if variant=='wrong_worker':scope['worker_pid']=43
            if variant=='missing_pid':scope['job_snapshots'][1]['pids'].remove(10)
            if variant=='creation':scope['membership_candidates'][0]['creation_ticks']=99
            with self.subTest(variant=variant),self.assertRaises(ValueError):identity.validate_resolution_scope(value,expected_worker_pid=42)

    def test_pure_outside_and_stale_exclusions_require_raw_positive_proofs(self):
        self.rows[99]=dict(pid=99,parent_pid=10);self.outside.add(99)
        outside=self.resolve_native()
        outside['endpoint_job_scope']['outside_job_edges'][0]['in_job_before']=True
        with self.assertRaises(ValueError):identity.validate_resolution_scope(outside)
        self.outside.clear();self.creations[99],self.exits[99]=800,0
        stale=self.resolve_native()
        stale['endpoint_job_scope']['stale_job_edges'][0]['parent_creation_ticks']=700
        with self.assertRaises(ValueError):identity.validate_resolution_scope(stale)


if __name__ == '__main__':
    unittest.main()
