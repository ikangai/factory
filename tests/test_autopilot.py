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


def test_restart_if_auto_respects_brakes_and_debounce(monkeypatch):
    """The AUTO watchdog (Task 5.3): restart a dead runner ONLY when AUTO ∧ ¬halted ∧ dead ∧
    debounce-elapsed. STOP wins over everything (checked FIRST); mode=shift and a live runner
    veto; the debounce stops a crash-looping runner from thrashing. No real process launched —
    start_runner is monkeypatched."""
    calls = []
    monkeypatch.setattr(autopilot, "start_runner",
                        lambda **k: calls.append(k) or {"started": True, "pid": 1})
    monkeypatch.setattr(autopilot, "runner_alive", lambda: None)          # runner is dead
    monkeypatch.setattr(autopilot.killswitch, "is_halted", lambda: False)
    monkeypatch.setattr(autopilot.mode, "is_auto", lambda: True)
    monkeypatch.setattr(autopilot, "_last_restart_ts", 0.0)

    # STOP wins over EVERYTHING — the brake is checked FIRST, before mode/liveness
    monkeypatch.setattr(autopilot.killswitch, "is_halted", lambda: True)
    r = autopilot.restart_if_auto(now=1000.0)
    assert r["restarted"] is False and r["reason"] == "halted" and not calls

    # mode=shift vetoes (human-in-the-loop)
    monkeypatch.setattr(autopilot.killswitch, "is_halted", lambda: False)
    monkeypatch.setattr(autopilot.mode, "is_auto", lambda: False)
    r = autopilot.restart_if_auto(now=1000.0)
    assert r["restarted"] is False and r["reason"] == "not_auto" and not calls

    # a still-alive runner → nothing to heal
    monkeypatch.setattr(autopilot.mode, "is_auto", lambda: True)
    monkeypatch.setattr(autopilot, "runner_alive", lambda: 4242)
    r = autopilot.restart_if_auto(now=1000.0)
    assert r["restarted"] is False and r["reason"] == "alive" and not calls

    # AUTO ∧ ¬halted ∧ dead ∧ debounce-elapsed → EXACTLY one restart
    monkeypatch.setattr(autopilot, "runner_alive", lambda: None)
    r = autopilot.restart_if_auto(now=1000.0)
    assert r["restarted"] is True and len(calls) == 1

    # a second poll INSIDE the debounce window → NO thrash
    r = autopilot.restart_if_auto(now=1010.0, debounce_sec=300.0)
    assert r["restarted"] is False and r["reason"] == "debounced" and len(calls) == 1

    # once the window elapses → heals again
    r = autopilot.restart_if_auto(now=1400.0, debounce_sec=300.0)
    assert r["restarted"] is True and len(calls) == 2


def test_fleet_state_poll_drives_the_watchdog(monkeypatch):
    """The dashboard's ~2s /api/fleet poll drives the self-heal: fleet_state() calls
    restart_if_auto(). Hermetic — the watchdog and store are stubbed, nothing spawns."""
    import types as _types
    from factory.dashboard import fleet_server

    seen = []
    monkeypatch.setattr(autopilot, "restart_if_auto", lambda: seen.append(True))
    monkeypatch.setattr(fleet_server, "fleet_viz",
                        _types.SimpleNamespace(fleet_json=lambda s: {"ok": True}))

    class _Store:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def init_db(self):
            pass

    monkeypatch.setattr(fleet_server, "Blackboard", lambda: _Store())
    assert fleet_server.fleet_state() == {"ok": True} and seen == [True]


def test_clear_pid_if_mine_only_removes_own(tmp_path, monkeypatch):
    pid_file = tmp_path / ".autopilot.pid"
    monkeypatch.setattr(autopilot, "pid_path", lambda: str(pid_file))
    pid_file.write_text("424242", encoding="utf-8")            # someone else's pid
    autopilot.clear_pid_if_mine()
    assert pid_file.exists()                                   # NOT mine → left alone
    pid_file.write_text(str(os.getpid()), encoding="utf-8")    # mine
    autopilot.clear_pid_if_mine()
    assert not pid_file.exists()                               # removed on my exit
