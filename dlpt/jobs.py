"""Background worker processes for the heavy PDF jobs (compress, estimate, split, extract).

MuPDF is not thread-safe, and its calls hold Python's GIL, so running them on a thread both risks crashes
and freezes the window. A small pool of worker processes avoids both. The workers only import
`dlpt.engine` and PyMuPDF, never the GUI.
"""

import concurrent.futures as cf
import multiprocessing
import os
import threading

_pool = None
_lock = threading.Lock()
WORKERS = 2


def _exit_with_parent(parent_pid):
    """Runs in each worker: quit as soon as the app that started it is gone, even if it crashed or was
    ended from Task Manager, so no worker is ever left running on its own."""
    def watch():
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(0x00100000, False, parent_pid)   # SYNCHRONIZE
            if handle:
                k32.WaitForSingleObject(handle, 0xFFFFFFFF)            # until the parent exits
        else:
            import time
            while os.getppid() == parent_pid:
                time.sleep(2)
        os._exit(0)
    threading.Thread(target=watch, daemon=True).start()


def _new_pool():
    return cf.ProcessPoolExecutor(max_workers=WORKERS, mp_context=multiprocessing.get_context("spawn"),
                                  initializer=_exit_with_parent, initargs=(os.getpid(),))


def pool():
    global _pool
    with _lock:
        if _pool is None:
            _pool = _new_pool()
        return _pool


def submit(fn, *args, **kwargs):
    """Run fn(*args, **kwargs) in a worker process; returns a concurrent.futures.Future."""
    global _pool
    try:
        return pool().submit(fn, *args, **kwargs)
    except cf.process.BrokenProcessPool:   # a worker crashed earlier: start a fresh pool
        with _lock:
            _pool = None
        return pool().submit(fn, *args, **kwargs)


def run(fn, *args, **kwargs):
    """Run in a worker and wait for the result (used by the web server's request threads)."""
    return submit(fn, *args, **kwargs).result()


def _noop():
    import pymupdf  # noqa: F401  (loads the library so the first real job starts faster)
    return True


def warm_up():
    for _ in range(WORKERS):
        submit(_noop)


def shutdown():
    """Stop the workers right away, abandoning unfinished estimates."""
    global _pool
    with _lock:
        p, _pool = _pool, None
    if p is None:
        return
    procs = list(getattr(p, "_processes", {}).values())
    p.shutdown(wait=False, cancel_futures=True)
    for proc in procs:
        try:
            proc.terminate()
        except Exception:
            pass
