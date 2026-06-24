from factory.checks.check_base import CheckContext, CheckResult
import re


def acceptance(ctx: CheckContext) -> CheckResult:
    src = ctx.read_file("transcript.txt") or ""
    expected = len(re.findall(r"\bmodel\b", src, flags=re.IGNORECASE))

    raw = ctx.read_file("answer.txt")
    if raw is None or raw.strip() == "":
        return CheckResult(False, "answer.txt absent/empty",
                           evidence={"expected": expected})

    got = raw.strip()
    if got != str(expected):
        return CheckResult(False,
                           f"answer.txt is {got!r}, recomputed expected {expected} "
                           f"(whole-word case-insensitive 'model' in transcript.txt)",
                           evidence={"expected": expected, "got": got,
                                     "transcript_head": src[:200]})

    return CheckResult(True,
                       f"answer.txt == {expected} (recomputed from transcript.txt)",
                       evidence={"expected": expected, "got": got})
