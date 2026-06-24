from factory.checks.check_base import CheckContext, CheckResult


def acceptance(ctx: CheckContext) -> CheckResult:
    expected = [
        "Hello and welcome to the show.",
        "Today we cover deterministic testing.",
        "Thanks for watching.",
    ]

    content = ctx.read_file("transcript.txt")
    if content is None:
        return CheckResult(
            False,
            "transcript.txt does not exist in the working directory",
            evidence={"transcript.txt": None},
        )

    if content.strip() == "":
        return CheckResult(
            False,
            "transcript.txt is empty (or only whitespace)",
            evidence={"raw": repr(content)},
        )

    # Allow a single trailing newline (or any trailing whitespace), but be strict
    # about the substantive content: exactly the three spoken lines in order, with
    # no blank lines, no WEBVTT header, and no timestamp/cue lines.
    raw_lines = content.split("\n")

    # Drop a single trailing empty element produced by a final newline.
    if raw_lines and raw_lines[-1] == "":
        raw_lines = raw_lines[:-1]

    # Normalize trailing whitespace on each line (tolerate trivial formatting),
    # but keep the lines themselves so we can detect stray blanks/headers.
    norm_lines = [ln.rstrip() for ln in raw_lines]

    evidence = {
        "raw": repr(content),
        "parsed_lines": norm_lines,
        "expected_lines": expected,
    }

    # No blank lines allowed in the body.
    if any(ln == "" for ln in norm_lines):
        return CheckResult(
            False,
            "transcript.txt contains blank line(s); blank lines must be stripped",
            evidence=evidence,
        )

    # Guard against leftover VTT artifacts even if line count happened to match.
    for ln in norm_lines:
        if ln == "WEBVTT" or ln.startswith("WEBVTT"):
            return CheckResult(
                False,
                "WEBVTT header line was not stripped",
                evidence=evidence,
            )
        if "-->" in ln:
            return CheckResult(
                False,
                f"timestamp/cue line was not stripped: {ln!r}",
                evidence=evidence,
            )

    if norm_lines != expected:
        return CheckResult(
            False,
            "transcript.txt does not match the expected three spoken lines in order",
            evidence=evidence,
        )

    return CheckResult(
        True,
        "transcript.txt contains exactly the three spoken caption lines in order, "
        "with header, timestamp lines, and blank lines removed",
        evidence=evidence,
    )
