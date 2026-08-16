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


# ==========================================================================================
# Cross-account invocation (drill 2 fallout, 2026-08-16). Once the guest-house home is 0700
# — which the same drill made it — `sudo -u factory bash …` from the operator's shell has
# two failure modes that both LOOK like the deployment is broken: bash cannot getcwd() from
# a directory the target account cannot traverse, and $HOME still points at the operator, so
# any script locating the tree through it lands in the wrong account.
# ==========================================================================================

def _code_lines(*parts: str) -> str:
    """Executable lines only. The scripts explain the bug they were fixed for in comments,
    which quotes the very code the contract below forbids."""
    return "\n".join(ln for ln in _read(*parts).splitlines()
                     if not ln.lstrip().startswith("#"))


def test_update_script_locates_the_tree_from_its_own_path_not_home():
    code = _code_lines("deploy", "user-factory", "update.sh")
    assert 'cd "$HOME/fab/factory"' not in code, "must not trust an inherited $HOME"
    assert "BASH_SOURCE" in code and 'cd "$ROOT"' in code


def test_update_script_warns_when_home_belongs_to_another_account():
    text = _read("deploy", "user-factory", "update.sh")
    assert "NFSHomeDirectory" in text
    assert "sudo -u factory -i" in text, "the fix must be printed, not just the diagnosis"


def test_update_script_documents_the_getcwd_failure_it_causes():
    """The operator hit this the first time and it reads like a broken deployment, not like
    a wrong invocation — so the message they will paste into a search engine is in the file."""
    text = _read("deploy", "user-factory", "update.sh")
    assert "getcwd" in text


def test_bootstrap_refuses_a_home_that_belongs_to_another_account():
    """This one must HARD-fail rather than warn: everything it writes is derived from $HOME,
    including the PAT env file, so a wrong destination is a credential in the wrong account."""
    text = _read("deploy", "user-factory", "02-bootstrap-as-factory.sh")
    guard = text.split("FAB=\"$HOME/fab\"", 1)[0]
    assert "NFSHomeDirectory" in guard
    assert "exit 1" in guard.split("ACCOUNT_HOME", 1)[1]


def test_update_script_still_syntax_checks():
    script = paths.factory("deploy", "user-factory", "update.sh")
    r = subprocess.run(["bash", "-n", script], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
