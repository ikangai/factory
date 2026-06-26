"""The autopilot supervisor: toggling AUTO must START a single detached runner (not just
arm a flag), be idempotent (no double-spawn), and self-heal a stale PID file. Hermetic —
the spawn is injected (no real process), and liveness uses this test's own PID."""
import os

from factory.orchestrator import autopilot


def test_start_runner_spawns_once_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "pid_path", lambda: str(tmp_path / ".autopilot.pid"))
    monkeypatch.setattr(autopilot, "_is_runner_proc", lambda pid: True)   # this test's pid stands in
    monkeypatch.setattr(autopilot, "_scan_for_runner", lambda: None)
    spawned = []

    def fake_spawn(argv, log_path):
        spawned.append(argv)
        return os.getpid()                      # a REAL, alive pid → runner_alive() sees it

    r1 = autopilot.start_runner(real=True, prod=False, spawn_fn=fake_spawn,
                                log_path=str(tmp_path / "ap.log"))
    assert r1["started"] and "--real" in r1["argv"] and "--prod" not in r1["argv"]
    assert autopilot.status() == {"running": True, "pid": os.getpid()}

    r2 = autopilot.start_runner(spawn_fn=fake_spawn, log_path=str(tmp_path / "ap.log"))
    assert not r2["started"] and r2["pid"] == os.getpid()   # already alive → no second spawn
    assert len(spawned) == 1                                 # exactly one runner


def test_runner_alive_cleans_a_stale_pid_file(tmp_path, monkeypatch):
    pid_file = tmp_path / ".autopilot.pid"
    pid_file.write_text("424242", encoding="utf-8")          # a pid past macOS' max → dead
    monkeypatch.setattr(autopilot, "pid_path", lambda: str(pid_file))
    assert autopilot.runner_alive() is None                  # dead → not running
    assert not pid_file.exists()                             # stale file removed


def test_start_adopts_existing_runner_when_pid_file_lost(tmp_path, monkeypatch):
    """Robustness: if the PID file is gone but a runner is alive (found via process scan),
    the toggle ADOPTS it instead of double-spawning — the bug that bit us live."""
    monkeypatch.setattr(autopilot, "pid_path", lambda: str(tmp_path / ".autopilot.pid"))  # no file
    monkeypatch.setattr(autopilot, "_scan_for_runner", lambda: 555)        # a runner found via scan
    spawned = []
    r = autopilot.start_runner(spawn_fn=lambda a, l: spawned.append(a) or 999)
    assert not r["started"] and r["pid"] == 555 and not spawned            # adopted, NOT respawned


def test_scan_for_runner_accepts_python_runner_rejects_shell(monkeypatch):
    import types
    seen = {}

    def fake_run(argv, **k):
        if argv[0] == "pgrep":
            return types.SimpleNamespace(stdout="777\n", returncode=0)
        if argv[0] == "ps":
            seen["ps_pid"] = argv[-1]
            return types.SimpleNamespace(
                stdout="python -m factory.orchestrator.orchestrator run --loop --real\n",
                returncode=0)
        return types.SimpleNamespace(stdout="", returncode=1)

    monkeypatch.setattr(autopilot, "_alive", lambda pid: True)
    monkeypatch.setattr(autopilot.subprocess, "run", fake_run)
    assert autopilot._scan_for_runner() == 777 and seen["ps_pid"] == "777"


def test_prod_flag_threads_into_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "pid_path", lambda: str(tmp_path / ".autopilot.pid"))
    monkeypatch.setattr(autopilot, "_scan_for_runner", lambda: None)
    captured = {}

    def fake_spawn(argv, log_path):
        captured["argv"] = argv
        return os.getpid()

    autopilot.start_runner(real=False, prod=True, spawn_fn=fake_spawn,
                           log_path=str(tmp_path / "ap.log"))
    assert "--prod" in captured["argv"] and "--real" not in captured["argv"]


def test_runner_alive_rejects_recycled_nonrunner_pid(tmp_path, monkeypatch):
    """Identity check: a live pid that is NOT a factory runner (recycled) is not 'running' —
    no phantom on the board, and a toggle won't adopt the wrong process."""
    pid_file = tmp_path / ".autopilot.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")    # alive, but it's pytest, not a runner
    monkeypatch.setattr(autopilot, "pid_path", lambda: str(pid_file))
    monkeypatch.setattr(autopilot, "_is_runner_proc", lambda pid: False)
    assert autopilot.runner_alive() is None and not pid_file.exists()


def test_cmd_autopilot_status_finds_runner_via_file_or_scan(monkeypatch):
    from factory.orchestrator import orchestrator
    monkeypatch.setattr(autopilot, "runner_alive", lambda: None)
    monkeypatch.setattr(autopilot, "_scan_for_runner", lambda: 4242)        # only the scan finds it
    assert orchestrator.cmd_autopilot("status") == {"running": True, "pid": 4242}
    monkeypatch.setattr(autopilot, "_scan_for_runner", lambda: None)
    assert orchestrator.cmd_autopilot("status") == {"running": False, "pid": None}


def test_clear_pid_if_mine_only_removes_own(tmp_path, monkeypatch):
    pid_file = tmp_path / ".autopilot.pid"
    monkeypatch.setattr(autopilot, "pid_path", lambda: str(pid_file))
    pid_file.write_text("424242", encoding="utf-8")            # someone else's pid
    autopilot.clear_pid_if_mine()
    assert pid_file.exists()                                   # NOT mine → left alone
    pid_file.write_text(str(os.getpid()), encoding="utf-8")    # mine
    autopilot.clear_pid_if_mine()
    assert not pid_file.exists()                               # removed on my exit
