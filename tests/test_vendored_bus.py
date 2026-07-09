"""The vendored agora bus: byte-pinned + functionally alive (design:
docs/plans/2026-07-08-factory-owned-bus-human-queue-design.md §1)."""
import hashlib
import os
import subprocess

from factory.common import paths

CHAT = os.path.join(paths.FACTORY_ROOT, "vendor", "agora", "chat.py")
# Update CONSCIOUSLY on re-vendor (see vendor/agora/VENDORED.md).
SHA256 = "76b039f45cd808b9e7289b40e899afe9d42f373f73addd62d20a209004936506"


def test_vendored_chat_is_byte_pinned():
    with open(CHAT, "rb") as fh:
        assert hashlib.sha256(fh.read()).hexdigest() == SHA256


def test_vendored_bus_round_trip(tmp_path):
    env = {**os.environ, "AGORA_DIR": str(tmp_path), "AGORA_SOLO_GRACE": "0"}
    # Send a message
    r = subprocess.run(["python3", CHAT, "send", "--from", "tester", "hello vendored bus"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "chat.db").exists()

    # Read back the message via log
    r = subprocess.run(["python3", CHAT, "log"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "hello vendored bus" in r.stdout
    assert "tester" in r.stdout
