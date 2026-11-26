"""Detached Linux wardrive process management."""

import fcntl
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

LOCK_NAME = "quartermaster.lock"
PID_NAME = "quartermaster.pid"
OUTPUT_NAME = "background.log"
WORKER_FLAG = "--_background-worker"
LOCK_ENV = "QUARTERMASTER_LOCK_FD"
READY_ENV = "QUARTERMASTER_READY_FD"
STARTUP_TIMEOUT = 15


def _paths(root):
    root = Path(root)
    return root / LOCK_NAME, root / PID_NAME, root / OUTPUT_NAME


def _worker_pid(pid):
    try:
        argv = Path("/proc/%d/cmdline" % pid).read_bytes().split(b"\0")
    except OSError:
        return False
    return WORKER_FLAG.encode("ascii") in argv


def is_running(root):
    lock_path, _, _ = _paths(root)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def run_if_idle(root, operation, busy_message, output=print):
    """Run a data operation under the collector lock, refusing to proceed if its state is unknown."""
    fd = None
    try:
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path, _, _ = _paths(root)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if fd is not None:
            os.close(fd)
        output(busy_message)
        return 1
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        output("Cannot safely access the Quartermaster lock: %s" % (exc.strerror or exc))
        return 1
    try:
        return operation()
    finally:
        os.close(fd)


def acquire_foreground(root, output=print):
    root = Path(root)
    lock_path, pid_path, _ = _paths(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        output("Quartermaster is already running.")
        return None
    temporary = pid_path.with_name("." + PID_NAME)
    temporary.write_text("%d\n" % os.getpid(), encoding="ascii")
    os.chmod(temporary, 0o600)
    os.replace(temporary, pid_path)
    return fd


def release_foreground(root, fd):
    _, pid_path, _ = _paths(root)
    try:
        if int(pid_path.read_text(encoding="ascii").strip()) == os.getpid():
            pid_path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
    os.close(fd)


def start(root, output=print, popen=subprocess.Popen):
    root = Path(root)
    lock_path, pid_path, log_path = _paths(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    ready_read = ready_write = None
    try:
        ready_read, ready_write = os.pipe()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                pid = int(pid_path.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                output("Quartermaster is already starting in the background.")
                return 0
            output("Quartermaster is already running (pid %d)." % pid)
            return 0

        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        log_fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            env = dict(os.environ, **{LOCK_ENV: str(fd), READY_ENV: str(ready_write)})
            process = popen([sys.executable, "-m", "quartermaster", WORKER_FLAG], stdin=subprocess.DEVNULL,
                            stdout=log_fd, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
                            pass_fds=(fd, ready_write), env=env)
        finally:
            os.close(log_fd)
        os.close(ready_write)
        ready_write = None
        temporary = pid_path.with_name("." + PID_NAME)
        temporary.write_text("%d\n" % process.pid, encoding="ascii")
        os.chmod(temporary, 0o600)
        os.replace(temporary, pid_path)
        readable, _, _ = select.select([ready_read], [], [], STARTUP_TIMEOUT)
        if not readable or os.read(ready_read, 1) != b"\x01":
            try:
                os.kill(process.pid, signal.SIGINT)
            except OSError:
                pass
            output("Quartermaster did not finish starting. Details: %s" % log_path)
            return 1
        output("Quartermaster started in the background (pid %d)." % process.pid)
        output("Logs: %s" % (root / "logs"))
        output("Stop: quartermaster --stop | Details: %s" % log_path)
        return 0
    except OSError as exc:
        output("Could not start Quartermaster: %s" % (exc.strerror or exc))
        return 1
    finally:
        os.close(fd)
        if ready_read is not None:
            os.close(ready_read)
        if ready_write is not None:
            os.close(ready_write)


def worker_ready():
    """Acknowledge successful collector startup to the detached launcher."""
    try:
        fd = int(os.environ.pop(READY_ENV))
    except (KeyError, ValueError):
        return
    try:
        os.write(fd, b"\x01")
    finally:
        os.close(fd)


def stop(root, output=print, kill=os.kill):
    _, pid_path, _ = _paths(root)
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        output("Quartermaster is not running in the background.")
        return 1
    if not _worker_pid(pid):
        if is_running(root):
            output("Quartermaster is running in the foreground; stop it with Ctrl-C.")
        else:
            try:
                pid_path.unlink()
            except OSError:
                pass
            output("No matching Quartermaster background process was found.")
        return 1
    try:
        kill(pid, signal.SIGINT)
    except ProcessLookupError:
        output("Quartermaster has already stopped.")
        return 1
    output("Stopping Quartermaster (pid %d); logs will be finalized." % pid)
    return 0


def status(root, output=print):
    lock_path, pid_path, _ = _paths(root)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        output("Cannot check Quartermaster status: %s" % (exc.strerror or exc))
        return 1
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                pid = int(pid_path.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                output("Quartermaster is starting in the background.")
                return 0
            output("Quartermaster is running (pid %d)." % pid)
            return 0
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        output("Quartermaster is not running in the background.")
        return 1
    finally:
        os.close(fd)


def worker_root(root, pid=None):
    """Hold the inherited launch lock and remove this worker's PID file on exit."""
    _, pid_path, _ = _paths(root)
    try:
        fd = int(os.environ.pop(LOCK_ENV))
    except (KeyError, ValueError):
        return None
    try:
        expected = os.getpid() if pid is None else pid
        for _ in range(500):
            try:
                if int(pid_path.read_text(encoding="ascii").strip()) == expected:
                    return fd
            except (OSError, ValueError):
                pass
            time.sleep(0.01)
        os.close(fd)
        return None
    except Exception:
        os.close(fd)
        raise


def finish_worker(root, fd, pid=None):
    expected = os.getpid() if pid is None else pid
    _, pid_path, _ = _paths(root)
    try:
        if int(pid_path.read_text(encoding="ascii").strip()) == expected:
            pid_path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
    if fd is not None:
        os.close(fd)
