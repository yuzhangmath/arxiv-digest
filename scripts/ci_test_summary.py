#!/usr/bin/env python3
"""Expose bounded pytest failures through GitHub Actions annotations."""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def _escape(value: str, *, limit: int, property_value: bool = False) -> str:
    escapes = {"%": "%25", "\r": "%0D", "\n": "%0A"}
    if property_value:
        escapes.update({",": "%2C", ":": "%3A"})
    tokens = [escapes.get(character, character) for character in value]
    escaped = "".join(tokens)
    if len(escaped) <= limit:
        return escaped

    def take(parts, budget):
        result = []
        for part in parts:
            if len(part) > budget:
                break
            result.append(part)
            budget -= len(part)
        return result

    marker = " ... "
    suffix = " [truncated]"
    budget = limit - len(marker) - len(suffix)
    # Keep both testcase context and the final exception at the traceback tail.
    head = take(tokens, budget // 4)
    tail = take(reversed(tokens), budget - budget // 4)
    return "".join(head) + marker + "".join(reversed(tail)) + suffix


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("Python test report is unavailable; see the earlier failed step.")
        return 0
    try:
        root = ET.parse(Path(arguments[0])).getroot()
    except (OSError, ET.ParseError):
        print("Python test report is unavailable; see the earlier failed step.")
        return 0
    emitted = 0
    for case in root.iter("testcase"):
        failures = [child for child in case if child.tag in {"failure", "error"}]
        if not failures:
            continue
        title = "::".join(value for value in (case.get("classname"), case.get("name")) if value)
        details = "\n".join(
            value for failure in failures
            for value in (failure.get("message"), failure.text) if value
        )
        print(
            f"::error title={_escape(title or 'Python test failure', limit=256, property_value=True)}::"
            f"{_escape(details or 'Python test failed.', limit=8000)}"
        )
        emitted += 1
        if emitted == 10:
            break
    if not emitted:
        print("Python test report contains no failed testcases; see the earlier failed step.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
