"""
Refuse to start a second copy of the app.

Alpaca's free tier allows exactly one live websocket per account. A second
instance doesn't fail cleanly — it takes the slot or gets rejected with
"connection limit exceeded", and both processes then thrash while neither
reliably receives bars. Two instances also mean two Executors polling Telegram
for the same confirmation, and only one of them will see your reply.

This uses an OS advisory lock on a file rather than a PID file, because the
lock is released by the kernel when the process dies — including a hard kill
or a power loss. A PID file left behind by a killed process would block every
future start until someone deleted it by hand.
"""
import os
import sys

if os.name == "nt":
    import msvcrt

    def _try_lock(handle) -> bool:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
else:
    import fcntl

    def _try_lock(handle) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False


# Held for the process lifetime. If this is garbage-collected the file closes
# and the lock is released, so the reference must outlive the function call.
_handle = None


def acquire(lock_path: str = "jarvis.lock") -> bool:
    """
    True if this process now owns the lock, False if another instance holds it.

    Nothing is written to the file. Both locking APIs lock a byte range at the
    current offset, and truncating or rewriting the file afterwards can drop
    that range on Windows — which silently lets a second instance through, the
    exact failure this is meant to prevent. The file's only job is to exist.
    """
    global _handle
    handle = open(lock_path, "a+")
    handle.seek(0)
    if not _try_lock(handle):
        handle.close()
        return False
    _handle = handle
    return True


def acquire_or_exit(lock_path: str = "jarvis.lock") -> None:
    if not acquire(lock_path):
        print(
            f"[main] another instance is already running (lock: {lock_path}). "
            f"Alpaca allows one websocket per account, so this copy is exiting "
            f"rather than fighting the running one for the connection.",
            file=sys.stderr,
        )
        raise SystemExit(1)
