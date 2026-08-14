"""The lock has to hold across *processes*, so these spawn real ones."""
import subprocess
import sys
import textwrap

import single_instance


def _child_tries_lock(lock_path: str, hold: bool = False) -> subprocess.CompletedProcess:
    """Run a separate interpreter that attempts to take the lock."""
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(sys.path[0])!r})
        import single_instance
        got = single_instance.acquire({str(lock_path)!r})
        print("GOT" if got else "BLOCKED")
        sys.stdout.flush()
        {"time.sleep(30)" if hold else ""}
    """)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)


def test_first_process_acquires(tmp_path):
    lock = str(tmp_path / "jarvis.lock")
    assert _child_tries_lock(lock).stdout.strip() == "GOT"


def test_second_process_is_blocked_while_first_holds(tmp_path):
    lock = str(tmp_path / "jarvis.lock")

    # Hold the lock in this process, then have a child try to take it.
    assert single_instance.acquire(lock) is True
    try:
        result = _child_tries_lock(lock)
        assert result.stdout.strip() == "BLOCKED", (
            f"a second instance took the lock while the first held it: {result.stdout!r}"
        )
    finally:
        single_instance._handle.close()
        single_instance._handle = None


def test_lock_is_released_when_the_holder_dies(tmp_path):
    """A PID file would go stale after a hard kill and block every future
    start. An OS lock is released by the kernel, so the next start succeeds."""
    lock = str(tmp_path / "jarvis.lock")

    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(sys.path[0])!r})
            import single_instance
            single_instance.acquire({str(lock)!r})
            print("HOLDING", flush=True)
            time.sleep(60)
        """)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "HOLDING"
        assert _child_tries_lock(lock).stdout.strip() == "BLOCKED"
    finally:
        holder.kill()          # hard kill, as if the machine cut power
        holder.wait(timeout=10)

    # The lock file still exists, but the lock itself is gone.
    assert _child_tries_lock(lock).stdout.strip() == "GOT"


def test_acquire_or_exit_raises_for_the_second_instance(tmp_path):
    lock = str(tmp_path / "jarvis.lock")
    assert single_instance.acquire(lock) is True
    try:
        code = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(sys.path[0])!r})
            import single_instance
            single_instance.acquire_or_exit({str(lock)!r})
            print("SHOULD NOT REACH")
        """)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        assert r.returncode == 1
        assert "already running" in r.stderr
        assert "SHOULD NOT REACH" not in r.stdout
    finally:
        single_instance._handle.close()
        single_instance._handle = None
