"""Task 8 (design: docs/plans/2026-07-08-factory-owned-bus-human-queue.md): `bin/factory bus`
is a thin passthrough to the vendored `vendor/agora/chat.py` so the operator can drive the
team bus from the CLI without knowing the vendor path. Mirrors tests/test_vendored_bus.py's
approach (real subprocess, isolated AGORA_DIR — never the real bus)."""
import os
import subprocess

from factory.common import paths

BIN = os.path.join(paths.FACTORY_ROOT, "bin", "factory")


def test_bin_factory_bus_syntax_is_valid():
    r = subprocess.run(["bash", "-n", BIN], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr


def test_bin_factory_bus_send_and_log_roundtrip(tmp_path):
    env = {**os.environ, "AGORA_DIR": str(tmp_path), "AGORA_SOLO_GRACE": "0"}
    r = subprocess.run([BIN, "bus", "send", "--from", "tester", "hello via bin/factory bus"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "chat.db").exists()

    r = subprocess.run([BIN, "bus", "log"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "hello via bin/factory bus" in r.stdout
    assert "tester" in r.stdout


def test_bin_factory_bus_preserves_an_operator_provided_agora_dir(tmp_path):
    """An explicit AGORA_DIR (e.g. a scratch bus) must win over the factory-bus default —
    the passthrough must not clobber it."""
    env = {**os.environ, "AGORA_DIR": str(tmp_path), "AGORA_SOLO_GRACE": "0"}
    subprocess.run([BIN, "bus", "send", "--from", "tester", "hi"],
                   capture_output=True, text=True, env=env, timeout=30, check=True)
    assert (tmp_path / "chat.db").exists()           # landed in OUR tmp dir, not the factory bus


def test_bin_factory_bus_defaults_to_the_factory_bus_dir_when_unset():
    """Without AGORA_DIR, the script must target the FACTORY's own bus dir (mirrors
    roles/common.py:factory_agora_dir() — prefer .agora, else .groupchat), never a cwd-relative
    or unset bus. Asserted by inspecting the script's default-path expression rather than
    running it unset, so this test never writes to the real bus."""
    with open(BIN, encoding="utf-8") as fh:
        text = fh.read()
    bus_arm = text.split("bus)", 1)[1].split(";;", 1)[0]
    assert '-z "${AGORA_DIR:-}"' in bus_arm                    # only defaults when unset
    assert '$HERE/../.agora' in bus_arm and '$HERE/../.groupchat' in bus_arm  # same two candidates
    assert 'vendor/agora/chat.py' in bus_arm                    # the vendored path, not a plugin
