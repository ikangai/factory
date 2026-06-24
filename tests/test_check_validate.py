"""Deterministic backstop for synthesized acceptance checks (factory bug #64).

A mined scenario's ORACLE is an LLM guess (the miner miscounted: claimed 15, the
real spoken-word count was 14). check-synth then bakes that guess into the check.
The dangerous shape gates the candidate on the guessed literal FIRST, with the
recompute-from-source guard AFTER it — so a CORRECT candidate writing the true
answer is failed by the wrong literal, and the guard can never fire.

`validate_synth_check` exercises a generated check against two synthetic
end-states built from the check's OWN recomputed oracle (evidence['expected']):
the correct answer must pass, a perturbed one must fail. A check that rejects its
own recomputed-correct answer (the bug) or accepts a wrong one is rejected —
caught deterministically, not left to the human gate. Checks it cannot exercise
(shell-based, or not exposing evidence['expected']) pass through as UNVERIFIED
rather than being falsely rejected.
"""
from factory.roles.check_validate import validate_synth_check

# captions.vtt whose real spoken-word count is 5 ("Hello world" + "Foo bar baz").
VTT = (
    "WEBVTT\n\n"
    "00:00:00.000 --> 00:00:01.000\nHello world\n\n"
    "00:00:01.000 --> 00:00:02.000\nFoo bar baz\n"
)
SCENARIO = {"goal": "count spoken words into wordcount.txt", "seed_files": {"captions.vtt": VTT}}

_RECOMPUTE = '''
def _expected(ctx):
    vtt = ctx.read_file("captions.vtt") or ""
    n = 0
    for line in vtt.splitlines():
        s = line.strip()
        if not s or s.startswith("WEBVTT") or "-->" in s:
            continue
        n += len(s.split())
    return n
'''

# GOOD: derives expected from the seed, compares the candidate output to it.
GOOD = _RECOMPUTE + '''
from factory.checks.check_base import CheckResult
def acceptance(ctx):
    expected = _expected(ctx)
    raw = ctx.read_file("wordcount.txt")
    if raw is None or raw.strip() == "":
        return CheckResult(False, "absent", evidence={"expected": expected})
    got = raw.strip()
    if got != str(expected):
        return CheckResult(False, "mismatch", evidence={"expected": expected, "got": got})
    return CheckResult(True, "ok", evidence={"expected": expected, "got": got})
'''

# BAD (#64): gates on a WRONG guessed literal "6" first; recompute (5) is dead code.
BAD_LITERAL_FIRST = _RECOMPUTE + '''
from factory.checks.check_base import CheckResult
def acceptance(ctx):
    raw = ctx.read_file("wordcount.txt")
    if raw is None:
        return CheckResult(False, "absent", evidence={"expected": _expected(ctx)})
    got = raw.strip()
    if got != "6":                       # WRONG literal (truth recomputes to 5)
        return CheckResult(False, "mismatch", evidence={"expected": _expected(ctx), "got": got})
    expected = _expected(ctx)
    if str(expected) != "6":             # guard ordered too late to ever help
        return CheckResult(False, "self", evidence={"expected": expected})
    return CheckResult(True, "ok", evidence={"expected": expected})
'''

# ACCEPTS WRONG: reads the output but passes any non-empty value.
ACCEPTS_WRONG = _RECOMPUTE + '''
from factory.checks.check_base import CheckResult
def acceptance(ctx):
    expected = _expected(ctx)
    raw = ctx.read_file("wordcount.txt")
    if raw and raw.strip():
        return CheckResult(True, "nonempty", evidence={"expected": expected})
    return CheckResult(False, "empty", evidence={"expected": expected})
'''

# No evidence['expected'] — cannot be exercised; must NOT be falsely rejected.
NO_EXPECTED = '''
from factory.checks.check_base import CheckResult
def acceptance(ctx):
    raw = ctx.read_file("wordcount.txt")
    ok = bool(raw) and raw.strip() == "5"
    return CheckResult(ok, "literal", evidence={"got": raw})
'''

# Shell-based — cannot be exercised deterministically; pass through as unverified.
SHELL = '''
from factory.checks.check_base import CheckResult
def acceptance(ctx):
    rc, out, err = ctx.run("wc -w < wordcount.txt")
    return CheckResult(True, "ok", evidence={"expected": 5})
'''


def test_good_recompute_first_check_validates():
    ok, reason = validate_synth_check(GOOD, SCENARIO)
    assert ok is True, reason
    assert "validated" in reason


def test_literal_first_wrong_oracle_is_rejected():
    ok, reason = validate_synth_check(BAD_LITERAL_FIRST, SCENARIO)
    assert ok is False
    assert "recomputed-correct" in reason


def test_check_accepting_wrong_answer_is_rejected():
    ok, reason = validate_synth_check(ACCEPTS_WRONG, SCENARIO)
    assert ok is False
    assert "wrong answer" in reason


def test_check_without_expected_evidence_is_unverified_not_rejected():
    ok, reason = validate_synth_check(NO_EXPECTED, SCENARIO)
    assert ok is True
    assert "unverified" in reason


def test_shell_based_check_is_unverified_not_rejected():
    ok, reason = validate_synth_check(SHELL, SCENARIO)
    assert ok is True
    assert "unverified" in reason
