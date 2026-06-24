from factory.checks.check_base import CheckContext, CheckResult


def acceptance(ctx: CheckContext) -> CheckResult:
    # Recompute the truth from the actual seed FIRST.
    text = ctx.read_file("text.txt")
    if text is None:
        return CheckResult(False, "text.txt seed missing", evidence={})
    body = text[:-1] if text.endswith("\n") else text  # drop one trailing newline
    expected = str(len(body))  # len() counts Unicode code points → 16 for the seed

    out = ctx.read_file("charcount.txt")
    if out is None:
        return CheckResult(False, "charcount.txt was not created",
                           evidence={"expected": expected})
    got = out.strip()
    if got != expected:
        return CheckResult(
            False,
            f"charcount.txt is {got!r}, expected {expected!r} "
            f"(code points, not bytes — byte count would be {len(body.encode('utf-8'))})",
            evidence={"expected": expected, "got": got,
                      "bytes": len(body.encode("utf-8"))},
        )
    return CheckResult(True, f"charcount.txt == {expected} (correct code-point count)",
                       evidence={"expected": expected, "got": got})
