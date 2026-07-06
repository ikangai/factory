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


# ============================================================================
# Task 3.1: execute the spec's NAMED acceptance test (stage='acceptance').
# extract_test_ref = conservative safe-charset regex; run_code_round runs the ref
# in the candidate AFTER the suite gate (red -> discard; missing -> telemetry skip).
# ============================================================================

# -- extract_test_ref (conservative, fail-open) ------------------------------
def test_extract_ref_plain_file():
    assert acceptance.extract_test_ref("tests/test_x.py") == "tests/test_x.py"


def test_extract_ref_with_nodeid_from_prose():
    assert acceptance.extract_test_ref(
        "the new test tests/test_retry.py::test_retries passes") == "tests/test_retry.py::test_retries"


def test_extract_ref_class_and_method_nodeid():
    assert acceptance.extract_test_ref(
        "tests/test_x.py::TestClass::test_method") == "tests/test_x.py::TestClass::test_method"


def test_extract_ref_from_spec_dict_acceptance():
    assert acceptance.extract_test_ref(
        {"acceptance": "prove it: tests/test_llm.py::test_backoff", "target_surface": "llm.py"}
    ) == "tests/test_llm.py::test_backoff"


def test_extract_ref_prose_returns_none():
    assert acceptance.extract_test_ref("a retry test passes and the backoff is respected") is None


def test_extract_ref_non_tests_path_returns_none():
    assert acceptance.extract_test_ref("edit src/clive/llm.py and add retry") is None


def test_extract_ref_rejects_pyc_and_extensions():
    assert acceptance.extract_test_ref("tests/test_x.pyc is stale") is None


def test_extract_ref_rejects_unsafe_charset():
    # a space breaks the token — no safe ref extracted (never returns a half path)
    assert acceptance.extract_test_ref("tests/foo bar.py") is None


def test_extract_ref_none_and_empty_inputs():
    assert acceptance.extract_test_ref(None) is None
    assert acceptance.extract_test_ref("") is None
    assert acceptance.extract_test_ref({}) is None


def test_extract_ref_rejects_parent_traversal():
    """A '..' path segment must never survive extraction — it would let pytest resolve a
    file OUTSIDE the candidate's tests/ dir (and, with enough '..', outside cand_repo into
    the operator's tree) and import/execute it. Fail-open to None."""
    assert acceptance.extract_test_ref("tests/../../etc/passwd.py") is None
    assert acceptance.extract_test_ref(
        "run tests/../../../operator/secret.py::test_boom please") is None
    assert acceptance.extract_test_ref("tests/a/../b/test_x.py::test_y") is None
    assert acceptance.extract_test_ref("tests/../secret.py") is None
    # a directory whose NAME merely contains dots is not a traversal segment — still OK
    assert acceptance.extract_test_ref("tests/a..b/test_x.py") == "tests/a..b/test_x.py"


# -- run_code_round: the acceptance-exec gate --------------------------------
class _AccAdapter:
    """Fake adapter that also implements the run_named_test seam (Task 3.1)."""
    def __init__(self, *, named=("passed", "acc ok"), tests_passed=True):
        self._named = named
        self._tests_passed = tests_passed
        self.ran_tests = False
        self.named_call = None

    def frozen_paths(self):
        return []

    def run_tests(self, repo, **k):
        self.ran_tests = True
        return (self._tests_passed, "suite report")

    def run_named_test(self, cwd, ref, **k):
        assert self.ran_tests, "acceptance ref must run AFTER the suite gate"
        self.named_call = (cwd, ref)
        return self._named

    def merge_branch(self, repo, branch, **k):
        return "MERGESHA"

    def current_commit(self, repo):
        return "HEAD"

    def revert_commit(self, repo, sha):
        return "REVERTSHA"


_TESTED_DIFF = ["src/clive/feature.py", "tests/test_feature.py"]


def _acc_round(adapter, ref):
    return code_round.run_code_round(
        adapter=adapter, main_repo="m", cand_repo="c", branch="b",
        champion_scores={"working": 0.8, "held_out": 0.7}, grade_fn=_graded,
        changed_paths=_TESTED_DIFF, acceptance_ref=ref)


def test_acceptance_red_run_discards_with_report():
    ad = _AccAdapter(named=("failed", "E   assert 1 == 2"))
    res = _acc_round(ad, "tests/test_feature.py::test_it")
    assert res["action"] == "discarded" and res["stage"] == "acceptance"
    assert "assert 1 == 2" in res["tests_report"]
    assert ad.named_call == ("c", "tests/test_feature.py::test_it")


def test_acceptance_green_run_merges():
    ad = _AccAdapter(named=("passed", "1 passed"))
    res = _acc_round(ad, "tests/test_feature.py::test_it")
    assert res["action"] == "merged"


def test_acceptance_missing_test_skips_and_flags_telemetry():
    """Correction (b): a MISSING named test (worker didn't create it) is a telemetry-first SKIP —
    the candidate is NOT discarded yet; acceptance_skipped rides out so the rail counts it."""
    ad = _AccAdapter(named=("missing", "no tests ran"))
    res = _acc_round(ad, "tests/test_feature.py::test_it")
    assert res["action"] == "merged"
    assert res["acceptance_skipped"] == "tests/test_feature.py::test_it"


def test_no_acceptance_ref_never_runs_named_test():
    ad = _AccAdapter(named=("failed", "would discard if called"))
    res = code_round.run_code_round(
        adapter=ad, main_repo="m", cand_repo="c", branch="b",
        champion_scores={"working": 0.8, "held_out": 0.7}, grade_fn=_graded,
        changed_paths=_TESTED_DIFF)                    # no acceptance_ref
    assert res["action"] == "merged" and ad.named_call is None


def test_acceptance_not_run_when_suite_is_red():
    ad = _AccAdapter(named=("passed", "x"), tests_passed=False)
    res = _acc_round(ad, "tests/test_feature.py::test_it")
    assert res["action"] == "discarded" and res["stage"] == "tests"
    assert ad.named_call is None                       # never reached the acceptance gate
