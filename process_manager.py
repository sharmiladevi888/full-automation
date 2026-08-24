"""Agent 5: Safe subprocess management. Kills zombies properly."""
import os
import signal
import subprocess
import sys
import time
from typing import Optional, List

class ProcessResult:
    def __init__(self, returncode, stdout, stderr, timed_out=False):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.ok = returncode == 0

def run_safe(args: List[str], timeout: int = 600, cwd: Optional[str] = None) -> ProcessResult:
    """Run subprocess with proper timeout + kill."""
    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "cwd": cwd}
    if sys.platform != "win32":
        kwargs["preexec_fn"] = os.setsid
    else:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(args, **kwargs)
    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
        return ProcessResult(
            proc.returncode,
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
        )
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=5)
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
        except Exception:
            stdout, stderr = "", ""
        return ProcessResult(124, stdout, stderr + f"\n[TIMEOUT] Killed after {timeout}s", True)
    except Exception as e:
        _kill_tree(proc)
        return ProcessResult(-1, "", str(e))

def _kill_tree(proc):
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            time.sleep(0.5)
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try: proc.kill()
        except Exception: pass
    try: proc.wait(timeout=5)
    except Exception: pass

def ffmpeg_run(args: List[str], timeout: int = 600) -> ProcessResult:
    full = list(args)
    if "-loglevel" not in full: full.extend(["-loglevel", "warning"])
    if "-y" not in full: full.append("-y")
    return run_safe(full, timeout=timeout)
