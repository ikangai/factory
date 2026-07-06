"""Regression tests for the xhigh review of the GSD integration (e69cea6..HEAD)."""
import os
import subprocess as _sp

from factory.common.store import Blackboard
from factory.common import config
from factory.orchestrator import develop
from factory.reporting import scope_check, acceptance, factory_memory
from factory.roles import common as roles_common


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


def _git_repo_with_diff(root, n_chars):
    """A real tiny git repo: base commit + a 'cand' branch adding an n_chars file. Returns base sha."""
    os.makedirs(root, exist_ok=True)
    _sp.run(["git", "init", "-q", root], check=True)
    _sp.run(["git", "-C", root, "config", "user.email", "t@t"], check=True)
    _sp.run(["git", "-C", root, "config", "user.name", "t"], check=True)
    with open(os.path.join(root, "seed.txt"), "w") as fh:
        fh.write("seed\n")
    _sp.run(["git", "-C", root, "add", "-A"], check=True)
    _sp.run(["git", "-C", root, "commit", "-qm", "base"], check=True)
    base = _sp.run(["git", "-C", root, "rev-parse", "HEAD"],
                   capture_output=True, text=True).stdout.strip()
    _sp.run(["git", "-C", root, "checkout", "-q", "-b", "cand"], check=True)
    with open(os.path.join(root, "big.txt"), "w") as fh:
        fh.write("x" * n_chars + "\n")
    _sp.run(["git", "-C", root, "add", "-A"], check=True)
    _sp.run(["git", "-C", root, "commit", "-qm", "cand"], check=True)
    return base


# == Task 2.2 (b): claude_p optional `model` param — shared plumbing =========
def test_isolated_argv_omits_model_by_default_byte_for_byte():
    # No model → the argv is IDENTICAL to today's (byte-for-byte; nothing else moves).
    assert roles_common._isolated_claude_argv(json_output=True) == [
        "claude", "-p", "--output-format", "json", "--setting-sources", "",
        "--tools", "", "--strict-mcp-config", "--mcp-config",
        roles_common._EMPTY_MCP_CONFIG]


def test_isolated_argv_appends_model_when_given():
    argv = roles_common._isolated_claude_argv(json_output=True, model="claude-haiku-4-5")
    i = argv.index("--model")
    assert argv[i + 1] == "claude-haiku-4-5"


def test_claude_p_threads_model_to_the_subprocess_argv(monkeypatch):
    seen = {}

    class _P:
        returncode = 0
        stdout = '{"result": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}}'

    monkeypatch.setattr(roles_common.subprocess, "run",
                        lambda argv, **k: (seen.update(argv=argv) or _P()))
    roles_common.claude_p("hi", model="claude-haiku-4-5")
    assert seen["argv"][seen["argv"].index("--model") + 1] == "claude-haiku-4-5"


def test_claude_p_no_model_omits_the_flag(monkeypatch):
    seen = {}

    class _P:
        returncode = 0
        stdout = '{"result": "ok", "usage": {}}'

    monkeypatch.setattr(roles_common.subprocess, "run",
                        lambda argv, **k: (seen.update(argv=argv) or _P()))
    roles_common.claude_p("hi")
    assert "--model" not in seen["argv"]


# == Task 2.2 (a): explicit truncation marker on the reviewer diff ===========
def test_review_diff_truncation_emits_marker(monkeypatch, tmp_path):
    root = str(tmp_path / "r")
    base = _git_repo_with_diff(root, 30000)          # diff > 20,000 chars → truncation fires
    seen = {}
    monkeypatch.setattr("factory.roles.common.claude_p",
                        lambda prompt, **k: (seen.update(prompt=prompt) or ('{"approve": true}', 1, 0.0)))
    develop._review_candidate(root, base, "cand", "do the thing")
    assert "[diff truncated at 20,000 of " in seen["prompt"]   # the reviewer KNOWS it graded a partial


def test_review_small_diff_has_no_truncation_marker(monkeypatch, tmp_path):
    root = str(tmp_path / "r")
    base = _git_repo_with_diff(root, 50)             # tiny diff → no truncation
    seen = {}
    monkeypatch.setattr("factory.roles.common.claude_p",
                        lambda prompt, **k: (seen.update(prompt=prompt) or ('{"approve": true}', 1, 0.0)))
    develop._review_candidate(root, base, "cand", "task")
    assert "diff truncated" not in seen["prompt"]
    assert "{SPEC}" not in seen["prompt"]            # the {SPEC} seam is resolved (folded into {TASK})


# == Task 2.2 (c): reviewer_tier resolved via resolve_model, threaded to claude_p
def test_reviewer_tier_threads_resolved_model(monkeypatch, tmp_path):
    fake_cfg = {"super_worker": {"reviewer_tier": "fast"},
                "models": {"frontier": "", "standard": "claude-sonnet-4-6", "fast": "claude-haiku-4-5"}}
    monkeypatch.setattr(config, "load_config", lambda: fake_cfg)
    root = str(tmp_path / "r")
    base = _git_repo_with_diff(root, 50)
    seen = {}
    monkeypatch.setattr("factory.roles.common.claude_p",
                        lambda prompt, **k: (seen.update(model=k.get("model")) or ('{"approve": true}', 1, 0.0)))
    develop._review_candidate(root, base, "cand", "task")
    assert seen["model"] == "claude-haiku-4-5"       # 'fast' resolved DOWN and threaded to claude_p


def test_reviewer_default_tier_is_frontier_empty_model(monkeypatch, tmp_path):
    fake_cfg = {"super_worker": {}, "models": {"frontier": "", "standard": "claude-sonnet-4-6"}}
    monkeypatch.setattr(config, "load_config", lambda: fake_cfg)
    root = str(tmp_path / "r")
    base = _git_repo_with_diff(root, 50)
    seen = {}
    monkeypatch.setattr("factory.roles.common.claude_p",
                        lambda prompt, **k: (seen.update(model=k.get("model")) or ('{"approve": true}', 1, 0.0)))
    develop._review_candidate(root, base, "cand", "task")
    assert seen["model"] == ""                       # '' default = frontier = account default (byte-for-byte)


# == Task 2.3 slice 1: a reviewer reject gets its OWN discard lesson =========
def test_lesson_for_block_review_stage_is_specific():
    """A reviewer reject (stage='review') must not collapse into the generic 'discarded'
    lesson — _DISCARD_BY_STAGE gains a 'review' entry so the blocked-task lesson names the
    reviewer as the cause and points at its reason."""
    review = factory_memory.lesson_for_block("discarded", "review")
    generic = factory_memory.lesson_for_block("discarded")
    assert review and review != generic
    assert "review" in review.lower()


# -- finding 1/6/11: prefilter must NOT clobber a durable authored spec ------
def test_prefilter_pass_without_spec_keeps_the_tasks_authored_spec(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-1", "do x", source="research",
                   spec={"target_surface": "llm.py", "acceptance": "retry test"})
        s.set_task_status("task-1", "in_progress", shift_id=sh)
        task = s.tasks_in_flight(sh)[0]                    # carries the persisted spec
        judge = lambda t: {"decision": "pass"}            # passes WITHOUT re-emitting a spec
        keep = scope_check.prefilter(s, [task], shift_id=sh, judge=judge)
        assert keep[0]["spec"]["target_surface"] == "llm.py"   # authored spec preserved, not {}


def test_prefilter_pass_with_spec_prefers_the_judge_spec(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        judge = lambda t: {"decision": "pass", "spec": {"target_surface": "fresh.py", "acceptance": "a"}}
        keep = scope_check.prefilter(s, [{"id": "t", "title": "x", "detail": "",
                                          "spec": {"target_surface": "old.py"}}],
                                     shift_id=sh, judge=judge)
        assert keep[0]["spec"]["target_surface"] == "fresh.py"


# -- finding 2/8: a source module named test_*.py is NOT a test --------------
def test_is_test_excludes_a_src_module_named_test_prefix():
    assert acceptance._is_test("tests/test_llm.py") is True
    assert acceptance._is_test("src/clive/tests/test_x.py") is True
    assert acceptance._is_test("src/clive/test_harness.py") is False   # production module, not a test
    ok, _ = acceptance.acceptance_ok(["src/clive/feature.py", "src/clive/test_harness.py"])
    assert ok is False                                    # untested source no longer slips through


# -- finding 3/9: _within_surface must not substring-match --------------------
def test_within_surface_rejects_substring_siblings():
    assert scope_check._within_surface("src/clive/llm.py", "llm.py") is True
    assert scope_check._within_surface("src/clive/rapid_api.py", "api.py") is False  # not a substring win
    assert scope_check._within_surface("src/clive/llm_utils.py", "llm") is False
    ok, _ = scope_check.spec_fulfillment({"target_surface": "api.py"},
                                         ["src/clive/api.py", "src/clive/rapid_api.py"])
    assert ok is False                                    # the real stray is now detected


# -- finding 5: add_subtasks robust to a non-list, strips, drops empty spec ---
def test_add_subtasks_non_list_subtasks_is_zero_not_chars(tmp_path):
    with _store(tmp_path) as s:
        assert scope_check.add_subtasks(s, "do x then y") == []   # a string is not iterated char-by-char
        assert s.list_tasks() == []


def test_add_subtasks_strips_detail_and_drops_empty_spec(tmp_path):
    with _store(tmp_path) as s:
        n = scope_check.add_subtasks(s, [{"title": "do x", "detail": "  spaced  "}])
        assert len(n) == 1
        t = s.list_tasks()[0]
        assert t["detail"] == "spaced"                    # stripped
        assert t["spec"] == {}                            # no surface/acceptance -> empty, not {"":""}
