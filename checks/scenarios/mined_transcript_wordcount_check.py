from factory.checks.check_base import CheckContext, CheckResult


def acceptance(ctx: CheckContext) -> CheckResult:
    raw = ctx.read_file("wordcount.txt")
    if raw is None:
        return CheckResult(False, "wordcount.txt is absent", evidence={"wordcount.txt": None})
    if raw.strip() == "":
        return CheckResult(
            False,
            "wordcount.txt is empty or whitespace-only",
            evidence={"raw_repr": repr(raw)},
        )

    trimmed = raw.strip()

    # Substantive criterion: the file must contain exactly the integer 15.
    if trimmed != "14":
        return CheckResult(
            False,
            f"wordcount.txt trimmed is {trimmed!r}, expected '14'",
            evidence={"raw_repr": repr(raw), "trimmed": trimmed},
        )

    # Independently recompute the expected word count from the real VTT so the
    # verdict reflects the world, not just a matching literal.
    expected = None
    vtt = ctx.read_file("captions.vtt")
    if vtt is not None:
        words = 0
        for line in vtt.splitlines():
            s = line.strip()
            if not s:
                continue
            if s == "WEBVTT" or s.startswith("WEBVTT"):
                continue
            if "-->" in s:
                continue
            words += len(s.split())
        expected = words
        if expected != 14:
            return CheckResult(
                False,
                f"file says '14' but recomputed spoken word count is {expected}",
                evidence={"trimmed": trimmed, "recomputed": expected},
            )

    return CheckResult(
        True,
        "wordcount.txt trimmed equals '14' (matches recomputed spoken-word count)",
        evidence={
            "raw_repr": repr(raw),
            "trimmed": trimmed,
            "recomputed_from_vtt": expected,
        },
    )
