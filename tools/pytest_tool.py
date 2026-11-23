"""pytest runner tool — executes the test suite in the workspace.

Registered in the `qa` toolset so QAEngineerAgent can run tests and get
structured results (pass/fail counts, coverage %, per-failure details)
without having to parse raw terminal output.
"""

from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.registry import registry
from tools._context import agent_env, get_workspace, safe_resolve

_MAX_OUTPUT_BYTES = 8_192


def _parse_junit_xml(xml_path: Path) -> Dict[str, Any]:
    """Parse a JUnit XML report into structured counts + failure details."""
    if not xml_path.exists():
        return {"passed": 0, "failed": 0, "errors": 0, "failures": []}
    try:
        root = ET.parse(xml_path).getroot()
        suites = list(root) if root.tag == "testsuites" else [root]

        passed = failed = errors = skipped = 0
        failures: List[Dict[str, str]] = []

        for suite in suites:
            t = int(suite.get("tests", 0))
            f = int(suite.get("failures", 0))
            e = int(suite.get("errors", 0))
            s = int(suite.get("skipped", 0))
            failed += f
            errors += e
            skipped += s
            passed += t - f - e - s

            for tc in suite.findall("testcase"):
                nodeid = f"{tc.get('classname', '')}.{tc.get('name', '')}".strip(".")
                for tag in ("failure", "error"):
                    elem = tc.find(tag)
                    if elem is not None:
                        failures.append(
                            {
                                "nodeid": nodeid,
                                "kind": tag,
                                "message": (elem.text or elem.get("message", ""))[:500],
                            }
                        )

        return {"passed": passed, "failed": failed, "errors": errors, "failures": failures}
    except ET.ParseError as exc:
        return {"passed": 0, "failed": 0, "errors": 0, "failures": [], "parse_error": str(exc)}


def _parse_coverage_json(cov_path: Path) -> float:
    """Extract overall line coverage % from a coverage.py JSON report."""
    if not cov_path.exists():
        return 0.0
    try:
        data = json.loads(cov_path.read_text(encoding="utf-8"))
        return round(data.get("totals", {}).get("percent_covered", 0.0), 1)
    except Exception:
        return 0.0


def run_pytest(
    directory: str = ".",
    args: Optional[List[str]] = None,
    xml_out: str = ".pytest_results.xml",
    cov_source: str = ".",
    timeout: int = 120,
) -> str:
    """Run pytest in the workspace directory and return structured results.

    Parameters
    ----------
    directory:
        Sub-path within the workspace where pytest should run.
    args:
        Extra pytest CLI arguments, e.g. ``["-k", "test_auth"]`` or ``["-x"]``.
    xml_out:
        Filename for the JUnit XML report (relative to *directory*).
    cov_source:
        Source path passed to ``--cov`` for coverage measurement.
    timeout:
        Maximum seconds before the subprocess is killed.

    Returns
    -------
    JSON string with keys: ``passed``, ``failed``, ``errors``, ``coverage_pct``,
    ``failures`` (list of ``{nodeid, kind, message}``), ``stdout``, ``return_code``.
    """
    workspace = get_workspace()
    cwd = safe_resolve(directory) if directory not in (".", "") else workspace
    xml_path = cwd / xml_out
    cov_path = cwd / ".coverage_report.json"

    cmd = [
        "python",
        "-m",
        "pytest",
        "--tb=short",
        f"--junitxml={xml_path}",
        f"--cov={cov_source}",
        f"--cov-report=json:{cov_path}",
    ]
    if args:
        cmd.extend(args)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=agent_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = proc.stdout[-_MAX_OUTPUT_BYTES:] if proc.stdout else ""
        return_code = proc.returncode
    except subprocess.TimeoutExpired:
        return json.dumps(
            {
                "error": f"pytest timed out after {timeout}s",
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "coverage_pct": 0.0,
                "failures": [],
                "stdout": "",
                "return_code": -1,
            }
        )
    except FileNotFoundError:
        return json.dumps(
            {
                "error": "python interpreter not found in PATH",
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "coverage_pct": 0.0,
                "failures": [],
                "stdout": "",
                "return_code": -1,
            }
        )

    # If pytest-cov is unavailable, coverage stays 0 — that is intentional
    junit = _parse_junit_xml(xml_path)
    coverage_pct = _parse_coverage_json(cov_path)

    return json.dumps(
        {
            "passed": junit["passed"],
            "failed": junit["failed"],
            "errors": junit["errors"],
            "coverage_pct": coverage_pct,
            "failures": junit.get("failures", []),
            "stdout": stdout,
            "return_code": return_code,
        },
        ensure_ascii=False,
    )


registry.register(
    name="run_pytest",
    handler=run_pytest,
    schema={
        "name": "run_pytest",
        "description": (
            "Run pytest in the workspace directory and return structured results: "
            "pass/fail/error counts, coverage percentage, and per-failure details "
            "(test node ID + error message). Use this instead of raw `terminal` calls "
            "when you need machine-readable test results."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Sub-path within workspace where pytest should run. Default: workspace root.",
                    "default": ".",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": 'Extra pytest CLI arguments, e.g. ["-k", "test_auth"] or ["-x"].',
                },
                "xml_out": {
                    "type": "string",
                    "description": "Filename for JUnit XML report (default: .pytest_results.xml).",
                    "default": ".pytest_results.xml",
                },
                "cov_source": {
                    "type": "string",
                    "description": "Source path for --cov coverage measurement (default: workspace root).",
                    "default": ".",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum seconds before pytest is killed (default: 120).",
                    "default": 120,
                },
            },
        },
    },
    toolset="qa",
)
