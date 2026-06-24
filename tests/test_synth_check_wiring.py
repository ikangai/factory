"""synth_check must not adopt a check that gates on a bad oracle (#64).

The deterministic backstop (check_validate) runs inside synth_check: a generated
check that fails its own recomputed-correct answer is written for human review but
the scenario is NOT pointed at it; a validated check is adopted as before.
"""
import yaml

from factory.common import paths
from factory.roles import common

VTT = ("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello world\n\n"
       "00:00:01.000 --> 00:00:02.000\nFoo bar baz\n")  # true spoken-word count = 5

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

GOOD = _RECOMPUTE + '''
from factory.checks.check_base import CheckResult
def acceptance(ctx):
    expected = _expected(ctx)
    raw = ctx.read_file("wordcount.txt")
    if raw is None or raw.strip() == "":
        return CheckResult(False, "absent", evidence={"expected": expected})
    if raw.strip() != str(expected):
        return CheckResult(False, "mismatch", evidence={"expected": expected})
    return CheckResult(True, "ok", evidence={"expected": expected})
'''

BAD = _RECOMPUTE + '''
from factory.checks.check_base import CheckResult
def acceptance(ctx):
    raw = ctx.read_file("wordcount.txt")
    if raw is None:
        return CheckResult(False, "absent", evidence={"expected": _expected(ctx)})
    if raw.strip() != "6":                 # WRONG guessed literal (truth recomputes to 5)
        return CheckResult(False, "mismatch", evidence={"expected": _expected(ctx)})
    return CheckResult(True, "ok", evidence={"expected": _expected(ctx)})
'''


def _stage(monkeypatch, tmp_path, code):
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(paths, "STAGING_DIR", str(staging))
    monkeypatch.setattr(paths, "FACTORY_ROOT", str(tmp_path))
    sid = "mined-wc"
    sc = {"id": sid, "goal": "count words into wordcount.txt",
          "seed_files": {"captions.vtt": VTT}, "check": "wordcount.txt == 6"}
    (staging / f"{sid}.yaml").write_text(yaml.safe_dump(sc))
    monkeypatch.setattr(common, "claude_p",
                        lambda prompt, **kw: ("```python\n" + code + "\n```", 0, 0.0))
    return sid, staging


def test_synth_check_rejects_literal_first_oracle(monkeypatch, tmp_path):
    sid, staging = _stage(monkeypatch, tmp_path, BAD)
    result = common.synth_check(None, sid)
    assert result is None
    sc = yaml.safe_load((staging / f"{sid}.yaml").read_text())
    assert "check" not in sc                       # scenario NOT pointed at the bad check
    assert "recomputed-correct" in sc["check_synth_rejected"]


def test_synth_check_adopts_validated_check(monkeypatch, tmp_path):
    sid, staging = _stage(monkeypatch, tmp_path, GOOD)
    result = common.synth_check(None, sid)
    assert result is not None
    sc = yaml.safe_load((staging / f"{sid}.yaml").read_text())
    assert sc["check"] == "checks/scenarios/mined_wc_check.py"
    assert "validated" in sc["check_validation"]
