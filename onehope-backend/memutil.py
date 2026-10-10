"""
memutil.py
──────────
Hands freed memory back to the operating system.

Python (via glibc malloc) keeps memory it has freed so it can reuse it, which
makes the process look bigger and bigger after each large operation such as
reading a big PDF or rebuilding the search index. On a 512 MB server that
slow creep is enough to get the service restarted, so we explicitly trim
after those operations.
"""

import ctypes
import gc
import logging

logger = logging.getLogger(__name__)

try:
    _libc = ctypes.CDLL("libc.so.6")
    _malloc_trim = _libc.malloc_trim
    _mallopt = _libc.mallopt
except (OSError, AttributeError):          # not glibc (e.g. macOS, Windows)
    _malloc_trim = _mallopt = None

_M_MMAP_THRESHOLD = -3
_M_ARENA_MAX = -8
_tuned = False


def tune_malloc() -> None:
    """
    Makes glibc give large freed blocks straight back to the OS (they are
    served by mmap), and limits per-thread memory pools. Measured on a
    simulated sync this lowers peak memory by ~90 MB. Equivalent to setting
    MALLOC_MMAP_THRESHOLD_=131072 and MALLOC_ARENA_MAX=2, but needs no
    configuration on the server.
    """
    global _tuned
    if _tuned or _mallopt is None:
        return
    _tuned = True
    try:
        _mallopt(_M_MMAP_THRESHOLD, 128 * 1024)
        _mallopt(_M_ARENA_MAX, 2)
    except Exception:
        pass


def release_memory() -> None:
    gc.collect()
    if _malloc_trim is not None:
        try:
            _malloc_trim(0)
        except Exception:                   # never let housekeeping break the app
            pass


def rss_mb() -> int:
    """Current resident memory in MB (Linux only; 0 elsewhere)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0
