"""Spec-bound acceptance gate (GSD integration #3, design:
docs/plans/2026-06-27-gsd-spec-driven-integration.md). A code change must ship a test."""
from factory.reporting import acceptance


# -- classification ----------------------------------------------------------
def test_is_test_path_recognizes_common_conventions():
    for p in ("tests/test_llm.py", "src/clive/tests/test_x.py", "foo_test.py",
              "pkg/widget.test.ts", "pkg/widget.spec.js"):
        assert acceptance._is_test(p), p


def test_is_source_path_excludes_tests_and_docs():
    assert acceptance._is_source("src/clive/llm.py")
    assert not acceptance._is_source("tests/test_llm.py")      # a test is not source-needing-a-test
    assert not acceptance._is_source("README.md")
    assert not acceptance._is_source("docs/guide.md")


# -- acceptance_ok -----------------------------------------------------------
def test_source_change_without_test_fails():
    ok, why = acceptance.acceptance_ok(["src/clive/llm.py"])
    assert ok is False and why


def test_source_change_with_test_passes():
    ok, _ = acceptance.acceptance_ok(["src/clive/llm.py", "tests/test_llm.py"])
    assert ok is True


def test_test_only_change_passes():
    assert acceptance.acceptance_ok(["tests/test_llm.py"])[0] is True


def test_docs_only_change_passes():
    assert acceptance.acceptance_ok(["README.md", "docs/guide.md"])[0] is True


def test_empty_diff_passes():
    assert acceptance.acceptance_ok([])[0] is True
    assert acceptance.acceptance_ok(None)[0] is True


# -- wiring into run_code_round (the gate) ------------------------------------
from factory.orchestrator import code_round


class _FakeAdapter:
    def frozen_paths(self):
        return []

    def run_tests(self, repo, **k):
        return (True, "ok")

    def merge_branch(self, repo, branch, **k):
        return "MERGESHA"

    def current_commit(self, repo):
        return "HEAD"

    def revert_commit(self, repo, sha):
        return "REVERTSHA"


def _graded(repo):
    return {"working": 0.9, "held_out": 0.7, "held_out_measured": True,
            "divergence_alarm": False, "safety_flag": False}


def _round(changed, require_test):
    return code_round.run_code_round(
        adapter=_FakeAdapter(), main_repo="m", cand_repo="c", branch="b",
        champion_scores={"working": 0.8, "held_out": 0.7}, grade_fn=_graded,
        changed_paths=changed, require_test=require_test)


def test_round_rejects_untested_source_when_require_test():
    res = _round(["src/clive/feature.py"], require_test=True)
    assert res["action"] == "discarded" and res["stage"] == "no_test"


def test_round_allows_untested_source_when_flag_off():
    res = _round(["src/clive/feature.py"], require_test=False)
    assert res["action"] == "merged"                  # gate unchanged when the flag is off


def test_round_allows_tested_source_when_require_test():
    res = _round(["src/clive/feature.py", "tests/test_feature.py"], require_test=True)
    assert res["action"] == "merged"
