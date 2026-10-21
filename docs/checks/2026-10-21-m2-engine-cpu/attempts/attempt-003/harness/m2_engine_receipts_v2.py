"""Require raw current-Job ownership proof before the unchanged CPU arithmetic.

The previous frozen scalar validator is reused on a shallow metadata copy with
only its method label adapted. Original receipts, raw stream and stored summary
are never rewritten. New Job scope is independently validated first.
"""
from copy import deepcopy

from m2_engine_identity_v2 import validate_resolution_scope
from m2_engine_receipts import validate_cell_cpu as validate_scalar_cpu
from m2_job_accounting import AccountingError


def validate_job_scopes(metadata):
    worker=metadata.get('affinity',{}).get('worker_pid')
    if type(worker) is not int or worker<=0:
        raise ValueError('CPU Job scope worker identity missing')
    processes=metadata.get('engine_cpu_processes')
    if not isinstance(processes,list) or len(processes)!=2:
        raise ValueError('two CPU endpoint Job scopes required')
    try:
        return [validate_resolution_scope(process['endpoint_resolution'],expected_worker_pid=worker)
                for process in processes]
    except (AccountingError, KeyError, TypeError) as issue:
        raise ValueError('CPU endpoint Job scope: '+str(issue)) from issue


def validate_cell_cpu(metadata,cell,policy=None):
    validate_job_scopes(metadata)
    adapted=dict(metadata)
    adapted['engine_cpu_processes']=deepcopy(metadata['engine_cpu_processes'])
    for process in adapted['engine_cpu_processes']:
        process['endpoint_resolution']['method']='frozen-image-and-held-creation-with-toolhelp-parent'
    return validate_scalar_cpu(adapted,cell,policy)
