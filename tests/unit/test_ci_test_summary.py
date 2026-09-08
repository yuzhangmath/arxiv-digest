from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from scripts import ci_test_summary


def test_failed_test_summaries_are_escaped_bounded_and_limited(tmp_path: Path, capsys) -> None:
    report = tmp_path / "report.xml"
    suite = ET.Element("testsuite")
    for index in range(12):
        case = ET.SubElement(suite, "testcase", classname="synthetic,class", name=f"failure:{index}%")
        ET.SubElement(case, "failure", message="Synthetic failure").text = (
            "bad%\r\n" + "x" * 20_000 + "\nFinal synthetic exception"
        )
    # An XML character reference preserves CR instead of normalizing CRLF to LF.
    report.write_text(ET.tostring(suite, encoding="unicode").replace("\r", "&#13;"))

    assert ci_test_summary.main([str(report)]) == 0
    annotations = [line for line in capsys.readouterr().out.splitlines() if line.startswith("::error ")]
    assert len(annotations) == 10
    assert annotations[0].startswith("::error title=synthetic%2Cclass%3A%3Afailure%3A0%25::")
    message = annotations[0].split("::", 2)[2]
    assert "bad%25%0D%0A" in message
    assert len(message) <= 8000
    assert "Final synthetic exception" in message
    assert message.endswith(" [truncated]")


def test_error_case_is_reported_and_passing_or_skipped_cases_are_omitted(tmp_path: Path, capsys) -> None:
    report = tmp_path / "report.xml"
    report.write_text(
        '<testsuites><testsuite><testcase name="passes"/>'
        '<testcase name="skips"><skipped/></testcase>'
        '<testcase name="broken"><error message="fixture error">Traceback</error></testcase>'
        '</testsuite></testsuites>'
    )
    assert ci_test_summary.main([str(report)]) == 0
    assert capsys.readouterr().out == "::error title=broken::fixture error%0ATraceback\n"


def test_missing_report_does_not_add_a_new_diagnostic_failure(tmp_path: Path, capsys) -> None:
    assert ci_test_summary.main([str(tmp_path / "missing.xml")]) == 0
    assert capsys.readouterr().out == "Python test report is unavailable; see the earlier failed step.\n"


def test_workflows_preserve_pytest_failure_and_only_summarize_failed_jobs() -> None:
    root = Path(__file__).resolve().parents[2]
    for name in ("tests.yml", "release.yml"):
        workflow = (root / ".github/workflows" / name).read_text()
        assert "continue-on-error:" not in workflow
        for label, tests, report, suite_name in (
            ("update-chain", "tests/integration/test_update_chain.py", "update-chain-tests.xml", "python"),
            ("Python", "tests/unit tests/integration", "python-tests.xml", "python"),
            ("browser", "tests/browser", "browser-tests.xml", "browser"),
        ):
            command = f'python -m pytest {tests} -q --junitxml="$RUNNER_TEMP/{report}"'
            assert command in workflow
            assert (
                f"      - name: Summarize {label} test failures\n"
                f"        if: failure() && matrix.suite == '{suite_name}'\n"
                f'        run: python scripts/ci_test_summary.py "$RUNNER_TEMP/{report}"\n'
            ) in workflow
            suite = workflow.index(command)
            summary = workflow.index(f"      - name: Summarize {label} test failures\n")
            assert suite < summary
