"""Re-export of the shared file-lock registry.

The implementation lives in :mod:`morning.locks` so the engine can use it too — a
low-level writer takes the lock itself, which is the only way a write can be guarded
no matter which of its callers performs it, and the engine must not import the web
layer.

Kept as a module because the server modules import ``file_lock`` from here. Importing
it from either place returns the same lock for the same path — there is one registry
in the process, and that is the entire point.
"""

from morning.locks import file_lock

__all__ = ["file_lock"]
