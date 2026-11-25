"""Offline entry point: python tests/offline/run.py [--full]. No installs."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import shutil
import xml.etree.ElementTree as ET


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.successes = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.successes.append(str(test))

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]


def fingerprints():
    paths = list((PROJECT / "tests/eval/datasets/chinatravel").rglob("*"))
    paths.append(PROJECT / "tests/eval/cases.json")
    return {str(p.relative_to(PROJECT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths if p.is_file()}


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="require project dependencies")
    parser.add_argument("--integration", action="store_true", help="run API/state tests as well (implies --full)")
    parser.add_argument("--report-dir", help="copy reports to this directory after the isolated worker exits")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--root", help=argparse.SUPPRESS)
    parser.add_argument("--probe", choices=("network", "write", "read"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.full = args.full or args.integration
    if not args.worker:
        root = Path(tempfile.mkdtemp(prefix="shanglv-offline-"))
        env = {k: v for k, v in os.environ.items()
               if not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
        env.update(PYTHONDONTWRITEBYTECODE="1", TMP=str(root), TEMP=str(root),
                   SHANGLV_TEST_ROOT=str(root), PI_DURABLE_ENABLED="0")
        cmd = [sys.executable, "-B", str(Path(__file__).resolve()), "--worker", "--root", str(root)]
        # Negative controls run in disposable subprocesses with the real audit
        # hook. They deliberately swallow the exception; the ledger must still
        # force a failing exit status, before running any application tests.
        for probe in ("network", "write", "read"):
            checked = subprocess.run(cmd + ["--probe", probe], env=env, cwd=root)
            probe_report = json.loads((root / f"probe-{probe}.json").read_text(encoding="utf-8"))
            if checked.returncode != 1 or not probe_report["violations"]:
                print(f"Isolation negative control failed: {probe}", file=sys.stderr)
                return 1
        if args.full:
            cmd.append("--full")
        if args.integration:
            cmd.append("--integration")
        result = subprocess.run(cmd, env=env, cwd=root)
        if args.report_dir:
            output = Path(args.report_dir).resolve()
            output.mkdir(parents=True, exist_ok=True)
            for name in ("report.json", "junit.xml", "summary.md"):
                if (root / name).exists():
                    shutil.copyfile(root / name, output / name)
        print(f"Reports: {root / 'report.json'}", flush=True)
        return result.returncode

    root = Path(args.root).resolve()
    sys.path.insert(0, str(PROJECT))
    sys.path.insert(0, str(HERE))
    from guards import Guard
    from unittest.mock import patch
    before = fingerprints()
    guard = Guard(root)
    guard.install()  # before importing any application code
    if args.probe:
        from guards import TestIsolationViolation
        try:
            if args.probe == "network":
                import socket
                with socket.socket() as sock:
                    sock.connect(("127.0.0.1", 9))
            elif args.probe == "write":
                (PROJECT / "forbidden-offline-probe.txt").write_text("must not be created")
            else:
                (PROJECT / "config.py").read_bytes()
        except TestIsolationViolation:
            pass
        (root / f"probe-{args.probe}.json").write_text(json.dumps({"violations": guard.violations}))
        return 1 if guard.violations else 0
    # Asyncio on Windows needs a socketpair for its wakeup pipe. A selector loop
    # avoids Proactor-specific native setup. No application sockets are permitted.
    import asyncio
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    import support
    support.GUARD = guard
    support.ROOT = root
    # urllib3 probes IPv6 by binding a socket during import. This offline
    # subprocess does not use IPv6; disable that probe, keeping bind/connect
    # guards fully active. No production environment is changed.
    import socket
    socket.has_ipv6 = False
    missing = [m for m in ("agentscope", "yaml", "ortools", "httpx")
               if importlib.util.find_spec(m) is None]
    if args.full and missing:
        print("FULL SUITE UNAVAILABLE (not passed): " + ", ".join(missing), file=sys.stderr)
    suite = unittest.defaultTestLoader.discover(str(HERE), pattern="test_core.py")
    if args.full and not missing:
        suite.addTests(unittest.defaultTestLoader.discover(str(HERE), pattern="test_full.py"))
        suite.addTests(unittest.defaultTestLoader.discover(str(HERE), pattern="test_maps.py"))
        suite.addTests(unittest.defaultTestLoader.discover(str(HERE), pattern="test_travel.py"))
    if args.integration:
        suite.addTests(unittest.defaultTestLoader.discover(str(HERE), pattern="test_integration.py"))
    with patch.dict(os.environ, {"OPENAI_API_KEY": "offline-no-credential"}):
        result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
    integrity_ok = before == fingerprints()
    report = {"scope": "offline code tests; not model evaluation", "tests_run": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors),
              "skipped": len(result.skipped), "isolation_violations": guard.violations,
              "isolation_details": guard.details,
              "data_unchanged": integrity_ok, "full_requested": args.full,
              "failed_tests": [str(t) for t, _ in result.failures + result.errors],
              "passed_tests": result.successes,
              "full_unavailable": missing if args.full else [],
              "integration_requested": args.integration,
              "failure_details": [{"test": str(t), "traceback": detail}
                                  for t, detail in result.failures + result.errors],
              "not_validated": ["real model quality", "real storage", "process crash recovery",
                                "full dependency tests" if not args.full or missing else "official benchmark"]}
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = result.wasSuccessful() and integrity_ok and not guard.violations and not (args.full and missing)
    xml = ET.Element("testsuite", name="offline-code", tests=str(result.testsRun),
                     failures=str(len(result.failures)), errors=str(len(result.errors)), skipped=str(len(result.skipped)))
    for name in result.successes:
        ET.SubElement(xml, "testcase", name=name)
    for kind, entries in (("failure", result.failures), ("error", result.errors)):
        for test, detail in entries:
            node = ET.SubElement(xml, "testcase", name=str(test))
            ET.SubElement(node, kind).text = detail
    for test, reason in result.skipped:
        ET.SubElement(ET.SubElement(xml, "testcase", name=str(test)), "skipped").text = reason
    if not integrity_ok or guard.violations or (args.full and missing):
        xml.set("errors", str(len(result.errors) + 1))
        xml.set("tests", str(result.testsRun + 1))
        ET.SubElement(ET.SubElement(xml, "testcase", name="run_integrity"), "error").text = "See report.json for isolation/dependency/integrity failure"
    ET.ElementTree(xml).write(root / "junit.xml", encoding="utf-8", xml_declaration=True)
    (root / "summary.md").write_text(
        f"# Offline code test results\n\nStatus: {'PASS' if ok else 'FAIL / UNAVAILABLE'}\n\n"
        f"Tests: {result.testsRun}; failures: {len(result.failures)}; errors: {len(result.errors)}; skipped: {len(result.skipped)}\n\n"
        f"Isolation violations: {len(guard.violations)}; source data unchanged: {integrity_ok}\n\n"
        + "\n".join(f"- {test}" for test, _ in result.failures + result.errors)
        + "\n\nNot a measurement of model quality. See report.json for details.\n", encoding="utf-8")
    print("OFFLINE RUN: " + ("PASS" if ok else "FAIL / UNAVAILABLE"), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
