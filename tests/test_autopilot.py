"""The autopilot supervisor: toggling AUTO must START a single detached runner (not just
arm a flag), be idempotent (no double-spawn), and self-heal a stale PID file. Hermetic —
the spawn is injected (no real process), and liveness uses this test's own PID."""
import os

from factory.orchestrator import autopilot


def test_start_runner_spawns_once_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "pid_path", lambda: str(tmp_path / ".autopilot.pid"))
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


def test_prod_flag_threads_into_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "pid_path", lambda: str(tmp_path / ".autopilot.pid"))
    captured = {}

    def fake_spawn(argv, log_path):
        captured["argv"] = argv
        return os.getpid()

    autopilot.start_runner(real=False, prod=True, spawn_fn=fake_spawn,
                           log_path=str(tmp_path / "ap.log"))
    assert "--prod" in captured["argv"] and "--real" not in captured["argv"]
