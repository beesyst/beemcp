from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_runner

ROOT = Path(__file__).resolve().parents[1]
PYTEST_VENV = ROOT.parent / "beeui" / ".venv"


class RunnerContractTests(unittest.TestCase):
    def test_disabled_and_unknown_suite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            projects = {"beeui": {}}
            settings = test_runner.validate_config(None, projects)
            runner = test_runner.LocalTestRunner(settings, lambda name: Path(temp), ())
            self.assertEqual(
                runner.start("beeui", "beeui_unit")["reason"], "test_runner_disabled"
            )
            settings["enabled"] = True
            settings["suites"] = {
                "beeui_unit": {"target": "beeui", "timeout_seconds": 5}
            }
            self.assertEqual(
                runner.start("unknown", "beeui_unit")["reason"],
                "target_or_suite_not_allowed",
            )
            self.assertEqual(
                runner.start("beeui", "../tests")["reason"],
                "target_or_suite_not_allowed",
            )
            self.assertEqual(runner.result("missing")["reason"], "job_not_found")

    def test_config_rejects_flags_and_unbounded_timeout(self) -> None:
        for invalid in (
            {"target": "beeui", "timeout_seconds": 3, "flags": ["-x"]},
            {"target": "beeui", "timeout_seconds": test_runner.MAX_TIMEOUT + 1},
        ):
            with self.assertRaises(RuntimeError):
                test_runner.validate_config(
                    {"suites": {"beeui_unit": invalid}}, {"beeui": {}}
                )

    def test_safe_report(self) -> None:
        output = (
            b"BEEMCP_TESTS_ENTERED\n/path/private KEY=hidden\nBEEMCP_RESULT 2 1 0 1\n"
        )
        result = test_runner._report(1, output, True, ("tests/test_local.py",))
        self.assertEqual(
            result,
            {
                "state": "failed",
                "exit_code": 1,
                "passed": 2,
                "failed": 1,
                "skipped": 0,
                "output_truncated": True,
                "reason": "test_failures",
                "failure_modules": ["tests/test_local.py"],
            },
        )


@unittest.skipUnless(
    os.environ.get("BEEMCP_SANDBOX_SMOKE") == "1",
    "requires user systemd and Bubblewrap",
)
class SandboxSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="beemcp-test-smoke-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.tests = self.root / "tests"
        self.tests.mkdir()
        self.test_file = self.tests / "test_smoke.py"
        (self.root / ".venv").symlink_to(PYTEST_VENV)
        with (self.root / ".env").open("w") as secret:
            secret.write("BEEMCP_SYNTHETIC_SECRET=do-not-mount")
        (self.root / ".env").chmod(0o600)
        (self.root / "original.txt").write_text("outside the snapshot")
        (self.tests / "escape.py").symlink_to(self.root / ".env")
        os.link(self.root / ".env", self.tests / "hardlink.py")
        self.git("init", "-q")
        tracked = self.tests / "tracked.py"
        tracked.write_text("old")
        self.git("add", "tests/tracked.py")
        tracked.write_text("dirty")
        suite = test_runner.Suite("beeui", ("tests/test_smoke.py",), loopback=True)
        self.enterContext(patch.dict(test_runner.SUITES, {"smoke": suite}))

    def git(self, *args: str) -> None:
        subprocess.run([test_runner.GIT, "-C", str(self.root), *args], check=True)

    def runner(self, timeout=20, **limits) -> test_runner.LocalTestRunner:
        settings = test_runner.validate_config(
            {
                "enabled": True,
                "suites": {"smoke": {"target": "beeui", "timeout_seconds": timeout}},
                "limits": limits,
            },
            {"beeui": {}},
        )
        return test_runner.LocalTestRunner(settings, lambda name: self.root, ())

    def wait(self, runner: test_runner.LocalTestRunner, job: dict) -> dict:
        deadline = time.monotonic() + 25
        while (result := runner.result(job["job_id"]))["state"] in {
            "queued",
            "running",
        }:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        return result

    def test_kernel_isolation_real_pytest_and_snapshot(self) -> None:
        self.test_file.write_text("""
import errno
import os
import resource
import socket
from pathlib import Path

import pytest

def test_isolation():
    assert Path("tests/tracked.py").read_text() == "dirty"
    assert not Path("tests/escape.py").exists()
    assert not Path("tests/hardlink.py").exists()
    assert not Path(".env").exists()
    assert not Path("/home/bee/Pro/beemcp/.env").exists()
    assert "SYNTHETIC_CREDENTIAL" not in os.environ
    assert Path("/proc/self/status").read_text().split("NoNewPrivs:")[1].split()[0] == "1"
    with pytest.raises(OSError) as error:
        Path("tests/tracked.py").write_text("changed")
    assert error.value.errno == errno.EROFS
    with pytest.raises(OSError) as error:
        Path("/forbidden").touch()
    assert error.value.errno == errno.EROFS
    with pytest.raises(OSError) as error:
        socket.socket(socket.AF_VSOCK)
    assert error.value.errno == errno.EPERM
    with socket.socket() as sock:
        assert sock.connect_ex(("192.0.2.1", 80)) == errno.ENETUNREACH
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == (80, 80)
    assert resource.getrlimit(resource.RLIMIT_FSIZE) == (1048576, 1048576)
    with pytest.raises(OSError):
        with Path("/tmp/large.bin").open("wb") as binary:
            binary.truncate(1048577)

def test_failed():
    assert False, "secret-looking fixture message"
""")
        runner = self.runner(open_files=80, file_mib=1)
        with patch.dict(os.environ, {"SYNTHETIC_CREDENTIAL": "not forwarded"}):
            queued = runner.start("beeui", "smoke")
            result = self.wait(runner, queued)
        self.assertEqual(result["snapshot_id"], queued["snapshot_id"])
        self.assertEqual(result["exit_code"], 1, result)
        self.assertEqual((result["passed"], result["failed"]), (1, 1), result)
        self.assertNotIn("secret-looking", str(result))
        self.assertEqual((self.tests / "tracked.py").read_text(), "dirty")
        self.assertEqual(
            (self.root / "original.txt").read_text(), "outside the snapshot"
        )
        (self.tests / "tracked.py").write_text("updated")
        second = self.wait(runner, runner.start("beeui", "smoke"))
        self.assertNotEqual(result["snapshot_sha256"], second["snapshot_sha256"])

    def test_busy_timeout_and_descendant_cleanup(self) -> None:
        self.test_file.write_text("""
import os
import subprocess
import sys
import time

def test_daemon():
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    if os.path.exists("/tmp/should_not_exist"):
        raise AssertionError
    time.sleep(60)
""")
        runner = self.runner(timeout=3)
        queued = runner.start("beeui", "smoke")
        self.assertEqual(
            runner.start("beeui", "smoke"), {"state": "refused", "reason": "busy"}
        )
        unit = f"beemcp-test-{queued['job_id']}.scope"
        manager = test_runner._manager_env()
        wait_until = time.monotonic() + 2.5
        pids = []
        while time.monotonic() < wait_until and not pids:
            group = subprocess.run(
                [
                    test_runner.SYSTEMCTL,
                    "--user",
                    "show",
                    unit,
                    "-p",
                    "ControlGroup",
                    "--value",
                ],
                env=manager,
                check=False,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if group:
                tasks = Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.procs"
                try:
                    seen = tasks.read_text().split()
                    if len(seen) >= 4:
                        self.assertEqual(
                            (tasks.parent / "pids.max").read_text().strip(), "64"
                        )
                        self.assertEqual(
                            (tasks.parent / "memory.max").read_text().strip(),
                            "805306368",
                        )
                        self.assertEqual(
                            (tasks.parent / "memory.swap.max").read_text().strip(), "0"
                        )
                        quota, period = (tasks.parent / "cpu.max").read_text().split()
                        self.assertEqual(int(quota), int(period))
                except FileNotFoundError:
                    seen = []
                if len(seen) >= 4:
                    pids = seen
            time.sleep(0.05)
        self.assertTrue(
            pids, "the running scope should contain the worker and its child"
        )
        result = self.wait(runner, queued)
        self.assertEqual(result["state"], "timeout", result)
        self.assertLess(result["duration_seconds"], 7)
        for pid in pids:
            process = Path("/proc") / pid / "stat"
            try:
                state = process.read_text().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                continue
            self.assertEqual(state, "Z", f"sandbox process {pid} survived")

    def test_early_failure_cleanup_and_fail_closed(self) -> None:
        self.test_file.write_text("""
import subprocess
import sys

def test_detached_child():
    subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
""")
        runner = self.runner()
        queued = runner.start("beeui", "smoke")
        result = self.wait(runner, queued)
        self.assertEqual(result["exit_code"], 0, result)
        active = subprocess.run(
            [
                test_runner.SYSTEMCTL,
                "--user",
                "is-active",
                f"beemcp-test-{queued['job_id']}.scope",
            ],
            env=test_runner._manager_env(),
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(active.returncode, 0)
        with patch.object(test_runner, "BWRAP", "/bin/false"):
            result = self.wait(runner, runner.start("beeui", "smoke"))
        self.assertEqual(result["state"], "refused", result)
        self.assertEqual(result["reason"], "unsupported_sandbox_requirement")
        self.assertNotIn("passed", result)
