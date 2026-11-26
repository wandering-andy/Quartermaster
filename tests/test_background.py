import fcntl
import os
import signal
from types import SimpleNamespace

from quartermaster import background


def test_background_start_detaches_once_and_reports_log_locations(tmp_path):
    held_fds = []
    output = []

    def popen(argv, **kwargs):
        assert argv[-1] == background.WORKER_FLAG
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == -3
        os.write(int(kwargs["env"][background.READY_ENV]), b"\x01")
        held_fds.append(os.dup(kwargs["pass_fds"][0]))
        return SimpleNamespace(pid=43210)

    try:
        assert background.start(tmp_path, output.append, popen) == 0
        assert (tmp_path / background.PID_NAME).read_text() == "43210\n"
        assert background.start(tmp_path, output.append, popen) == 0
        assert output == [
            "Quartermaster started in the background (pid 43210).",
            "Logs: %s" % (tmp_path / "logs"),
            "Stop: quartermaster --stop | Details: %s" % (tmp_path / background.OUTPUT_NAME),
            "Quartermaster is already running (pid 43210).",
        ]
    finally:
        for fd in held_fds:
            os.close(fd)


def test_background_start_does_not_report_success_without_readiness(tmp_path):
    output = []
    signals = []

    def popen(argv, **kwargs):
        return SimpleNamespace(pid=43211)

    original_kill = background.os.kill
    background.os.kill = lambda pid, sig: signals.append((pid, sig))
    try:
        assert background.start(tmp_path, output.append, popen) == 1
    finally:
        background.os.kill = original_kill

    assert signals == [(43211, signal.SIGINT)]
    assert output == ["Quartermaster did not finish starting. Details: %s" %
                      (tmp_path / background.OUTPUT_NAME)]


def test_stop_sends_sigint_only_to_matching_worker(tmp_path, monkeypatch):
    pid_path = tmp_path / background.PID_NAME
    pid_path.write_text("43210\n")
    sent = []
    output = []
    monkeypatch.setattr(background, "_worker_pid", lambda pid: pid == 43210)

    assert background.stop(tmp_path, output.append, lambda pid, sig: sent.append((pid, sig))) == 0
    assert sent == [(43210, signal.SIGINT)]
    assert output == ["Stopping Quartermaster (pid 43210); logs will be finalized."]


def test_status_uses_lock_and_removes_stale_pid(tmp_path):
    lock_path = tmp_path / background.LOCK_NAME
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    pid_path = tmp_path / background.PID_NAME
    pid_path.write_text("43210\n")
    output = []
    try:
        assert background.status(tmp_path, output.append) == 0
        assert output == ["Quartermaster is running (pid 43210)."]
    finally:
        os.close(fd)

    output.clear()
    assert background.status(tmp_path, output.append) == 1
    assert output == ["Quartermaster is not running in the background."]
    assert not pid_path.exists()


def test_foreground_lock_blocks_second_instance_and_clears_pid(tmp_path):
    output = []
    fd = background.acquire_foreground(tmp_path, output.append)
    assert fd is not None
    assert background.is_running(tmp_path)
    assert (tmp_path / background.PID_NAME).read_text() == "%d\n" % os.getpid()
    assert background.acquire_foreground(tmp_path, output.append) is None
    assert output == ["Quartermaster is already running."]
    background.release_foreground(tmp_path, fd)
    assert not background.is_running(tmp_path)
    assert not (tmp_path / background.PID_NAME).exists()


def test_stop_does_not_remove_foreground_pid(tmp_path, monkeypatch):
    pid_path = tmp_path / background.PID_NAME
    pid_path.write_text("43210\n")
    output = []
    monkeypatch.setattr(background, "_worker_pid", lambda pid: False)
    monkeypatch.setattr(background, "is_running", lambda root: True)

    assert background.stop(tmp_path, output.append) == 1
    assert pid_path.read_text() == "43210\n"
    assert output == ["Quartermaster is running in the foreground; stop it with Ctrl-C."]
