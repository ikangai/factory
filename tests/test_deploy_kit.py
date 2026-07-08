"""Task 8 (design: docs/plans/2026-07-08-factory-owned-bus-human-queue.md): the deployment
kit must stop telling the operator to install the agora plugin (the bus is now vendored into
the repo — nothing to install) and must document the human queue as part of steering.
Hermetic: reads the shipped kit files directly off disk, no shell/daemon spun up (the only
subprocess is a read-only `bash -n` syntax pass)."""
import subprocess

from factory.common import paths


def _read(*parts: str) -> str:
    with open(paths.factory(*parts), encoding="utf-8") as fh:
        return fh.read()


def test_bootstrap_script_drops_the_agora_plugin_install_step():
    text = _read("deploy", "user-factory", "02-bootstrap-as-factory.sh")
    assert "agora plugin" not in text
    assert "claude login" in text                    # the real manual step stays


def test_bootstrap_script_still_syntax_checks():
    """A real `bash -n` pass — same discipline as tests/test_bin_factory_bus.py — so an edit
    to the bootstrap script can never ship a shell syntax error."""
    script = paths.factory("deploy", "user-factory", "02-bootstrap-as-factory.sh")
    r = subprocess.run(["bash", "-n", script], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr


def test_runbook_drops_the_agora_plugin_install_step():
    text = _read("docs", "runbooks", "factory-user-deployment.md")
    assert "agora plugin marketplace" not in text
    assert "plugin marketplace add" not in text
    assert "vendor/agora/chat.py" in text              # replaced with: it's vendored, nothing to install


def test_runbook_documents_the_human_queue_in_steering():
    text = _read("docs", "runbooks", "factory-user-deployment.md")
    steering = text.split("## 6. Steering", 1)[1].split("## 7.", 1)[0]
    assert "Human queue" in steering
    assert "Queue" in steering and "@human" in steering
    assert "push_approval" in steering                 # names the config brake gating GitHub pushes
    assert "stale" in steering                          # the ~3-day staleness flag
