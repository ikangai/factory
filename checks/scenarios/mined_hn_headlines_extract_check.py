from factory.checks.check_base import CheckContext, CheckResult

EXPECTED = [
    "Rust 2.0 ships ownership-free mode",
    "Show HN: A deterministic test harness",
    "The case against microservices",
    "SQLite is faster than you think",
]


def acceptance(ctx: CheckContext) -> CheckResult:
    content = ctx.read_file("headlines.txt")
    if content is None:
        return CheckResult(False, "headlines.txt does not exist", evidence={"headlines.txt": None})

    if content.strip() == "":
        return CheckResult(
            False,
            "headlines.txt is empty",
            evidence={"raw": content},
        )

    # Allow a single trailing newline (and tolerate trailing whitespace per line),
    # but be strict on the substantive content and ordering.
    # Strip only the trailing newline(s) at end of file, then split.
    body = content.rstrip("\n")
    lines = body.split("\n")
    # Tolerate trailing whitespace on individual lines.
    normalized = [ln.rstrip() for ln in lines]

    if normalized == EXPECTED:
        return CheckResult(
            True,
            "headlines.txt contains exactly the four expected headlines in document order",
            evidence={
                "lines": normalized,
                "line_count": len(normalized),
                "raw_head": content[:500],
            },
        )

    # Diagnose the mismatch.
    if len(normalized) != len(EXPECTED):
        detail = (
            f"expected {len(EXPECTED)} lines, found {len(normalized)}"
        )
    else:
        diffs = [
            f"line {i+1}: expected {EXPECTED[i]!r}, found {normalized[i]!r}"
            for i in range(len(EXPECTED))
            if normalized[i] != EXPECTED[i]
        ]
        detail = "content mismatch: " + "; ".join(diffs)

    return CheckResult(
        False,
        detail,
        evidence={
            "expected": EXPECTED,
            "found": normalized,
            "found_count": len(normalized),
            "raw_head": content[:500],
        },
    )
