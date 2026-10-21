"""Bound the caller's wait for a potentially blocked owned-Job operation.

The daemon executes the supplied operation exactly once. A timeout does not
cancel that operation, prove cleanup, or authorize closing a handle another
thread still uses. The runner must mark its result invalid and retain uncertain
cleanup state. WindowsOwnedJob's lock serializes eventual native work/close;
only newly owned workers belong to that Job.
"""
import math
import threading


def bounded_job_call(method, *args, timeout_seconds, **kwargs):
    """Return the operation's value, propagate its error, or stop waiting.

    The bound covers waiting after thread creation, not native completion or
    cancellation. No argument values or returned data are included in errors.
    A caller interrupted while waiting also leaves the daemon operation alone.
    """
    if not callable(method):
        raise TypeError('an owned-Job callable is required')
    if (type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0
            or timeout_seconds > threading.TIMEOUT_MAX):
        raise ValueError('timeout_seconds must be positive, finite and supported')

    completed, reply = threading.Event(), {}

    def invoke():
        try:
            reply['value'] = method(*args, **kwargs)
        except BaseException as error:
            reply['error'] = error
        finally:
            completed.set()

    worker = threading.Thread(target=invoke, name='M2OwnedJobOperation', daemon=True)
    worker.start()
    if not completed.wait(timeout_seconds):
        raise TimeoutError('owned Job operation exceeded the caller response bound; '
                           'operation may still be running and cleanup is unconfirmed')
    if 'error' in reply:
        raise reply['error']
    return reply['value']
