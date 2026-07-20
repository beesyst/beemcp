from __future__ import annotations

import os
import asyncio
import importlib.util
import io
import shutil
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import AsyncMock, patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def writable_tmp_root() -> Path | None:
    candidates = [Path("/dev/shm")]
    candidates.extend(
        Path(value)
        for name in ("TMPDIR", "TEMP", "TMP")
        if (value := os.environ.get(name))
    )
    candidates.extend((Path("/tmp"), Path("/var/tmp")))
    for candidate in dict.fromkeys(candidates):
        if not candidate.is_dir():
            continue
        try:
            probe = Path(tempfile.mkdtemp(prefix="bee-dev-mcp-probe-", dir=candidate))
        except OSError:
            continue
        probe.rmdir()
        return candidate
    return None


TMP_ROOT = writable_tmp_root()


class StartScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        if TMP_ROOT is None:
            self.skipTest("no writable temporary storage is available")
        self.temp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.repo = base / "staged repo"
        self.home = base / "home"
        self.bin = base / "bin"
        self.log = base / "calls.log"
        (self.repo / "ops/systemd").mkdir(parents=True)
        (self.repo / ".venv/bin").mkdir(parents=True)
        (self.home / ".config/bee-dev-mcp").mkdir(parents=True)
        self.bin.mkdir()
        shutil.copy2(ROOT / "start.sh", self.repo / "start.sh")
        for name in ("bee-dev-mcp.service", "bee-dev-mcp-tunnel.service"):
            shutil.copy2(ROOT / "ops/systemd" / name, self.repo / "ops/systemd" / name)
        self.python = self.repo / ".venv/bin/python"
        self.python.write_text(
            '#!/bin/sh\necho "python $*" >> "$CALL_LOG"\n'
            'if [ "${FAIL_TOOLS:-0}" = 0 ]; then\n'
            '  echo "OK: all 8 expected tools found"\n'
            '  echo "tool_contract_sha256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"\n'
            '  exit 0\n'
            'fi\n'
            'exit 1\n',
            encoding="utf-8",
        )
        self.python.chmod(0o755)
        (self.home / ".config/bee-dev-mcp/tunnel.env").write_text(
            "TUNNEL_SECRET=also-never-print-this\n", encoding="utf-8"
        )
        self._mock("systemctl", r'''#!/bin/sh
echo "systemctl $*" >> "$CALL_LOG"
case " $* " in
  *" show-environment "*) [ "${FAIL_MANAGER:-0}" = 0 ] ;;
  *" is-active bee-dev-mcp.service "*) [ "${FAIL_MCP_ACTIVE:-0}" = 0 ] ;;
  *" is-active bee-dev-mcp-tunnel.service "*) [ "${FAIL_TUNNEL_ACTIVE:-0}" = 0 ] ;;
  *" status bee-dev-mcp.service "*) [ "${FAIL_MCP_STATUS:-0}" = 0 ] ;;
  *" status bee-dev-mcp-tunnel.service "*) [ "${FAIL_TUNNEL_STATUS:-0}" = 0 ] ;;
  *) exit 0 ;;
esac
''')
        self._mock("journalctl", '#!/bin/sh\necho "journalctl $*" >> "$CALL_LOG"\n')
        self._mock("uv", r'''#!/bin/sh
echo "uv cwd=$PWD $*" >> "$CALL_LOG"
mkdir -p .venv/bin
printf '#!/bin/sh\nexit 0\n' > .venv/bin/python
chmod +x .venv/bin/python
''')
        self._mock("curl", r'''#!/bin/sh
echo "curl $*" >> "$CALL_LOG"
case "$*" in
  *readyz*) [ "${FAIL_READY:-0}" = 0 ] ;;
  *healthz*) [ "${FAIL_HEALTH:-0}" = 0 ] ;;
  *versionz*)
    [ "${FAIL_VERSIONZ:-0}" = 0 ] && \
      echo '{"service":"bee-dev-mcp","git_head":"abc123","tool_contract_version":2,"tool_count":8,"tool_contract_sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}'
    [ "${FAIL_VERSIONZ:-0}" = 0 ]
    ;;
  *) exit 0 ;;
esac
''')
        self._mock("tunnel-client", '#!/bin/sh\nexit 0\n')

    def _mock(self, name: str, text: str) -> None:
        path = self.bin / name
        path.write_text(text, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def run_script(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            HOME=str(self.home),
            PATH=f"{self.bin}:{env['PATH']}",
            CALL_LOG=str(self.log),
        )
        env.update(extra)
        return subprocess.run(
            [str(self.repo / "start.sh"), *args],
            cwd=Path(self.temp.name),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def install_units(self) -> None:
        target = self.home / ".config/systemd/user"
        target.mkdir(parents=True, exist_ok=True)
        for name in ("bee-dev-mcp.service", "bee-dev-mcp-tunnel.service"):
            shutil.copy2(self.repo / "ops/systemd" / name, target / name)

    def test_setup_from_other_cwd_installs_both_units_without_starting(self) -> None:
        result = self.run_script("setup")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertIn(f"uv cwd={self.repo} sync --frozen", calls)
        self.assertIn("systemctl --user daemon-reload", calls)
        self.assertFalse(any(" start " in f" {call} " for call in calls))
        target = self.home / ".config/systemd/user"
        for name in ("bee-dev-mcp.service", "bee-dev-mcp-tunnel.service"):
            self.assertTrue((target / name).is_file())
            self.assertEqual(stat.S_IMODE((target / name).stat().st_mode), 0o644)

    def test_default_and_up_skip_setup_when_already_installed(self) -> None:
        self.install_units()
        for args in ((), ("up",)):
            self.log.unlink(missing_ok=True)
            result = self.run_script(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = self.calls()
            self.assertFalse(any(call.startswith("uv ") for call in calls))
            mcp = calls.index("systemctl --user start bee-dev-mcp.service")
            ready = next(i for i, call in enumerate(calls) if "readyz" in call)
            tunnel = calls.index("systemctl --user start bee-dev-mcp-tunnel.service")
            self.assertLess(mcp, ready)
            self.assertLess(ready, tunnel)

    def test_up_runs_setup_only_when_prerequisite_missing(self) -> None:
        self.install_units()
        self.python.unlink()
        result = self.run_script("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(call.startswith("uv ") for call in self.calls()))

    def test_failed_readiness_does_not_start_tunnel_and_shows_log_tail(self) -> None:
        self.install_units()
        self._mock("sleep", "#!/bin/sh\nexit 0\n")
        result = self.run_script("up", FAIL_READY="1")
        self.assertNotEqual(result.returncode, 0)
        calls = self.calls()
        self.assertNotIn("systemctl --user start bee-dev-mcp-tunnel.service", calls)
        self.assertIn("journalctl --user -u bee-dev-mcp.service --no-pager -n 40", calls)

    def test_restart_orders_stops_then_mcp_ready_tunnel_and_checks_both(self) -> None:
        self.install_units()
        result = self.run_script("restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        expected = [
            "systemctl --user stop bee-dev-mcp-tunnel.service",
            "systemctl --user stop bee-dev-mcp.service",
            "systemctl --user start bee-dev-mcp.service",
        ]
        self.assertEqual(calls[:3], expected)
        ready = next(i for i, call in enumerate(calls) if "readyz" in call)
        tunnel = calls.index("systemctl --user start bee-dev-mcp-tunnel.service")
        self.assertLess(ready, tunnel)
        self.assertIn("systemctl --user is-active bee-dev-mcp.service", calls)
        self.assertIn("systemctl --user is-active bee-dev-mcp-tunnel.service", calls)

    def test_check_tools_uses_derived_url_and_precedes_tunnel(self) -> None:
        self.install_units()
        result = self.run_script("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        check = next(i for i, call in enumerate(calls) if call.startswith("python "))
        tunnel = calls.index("systemctl --user start bee-dev-mcp-tunnel.service")
        self.assertIn("-B", calls[check])
        self.assertIn("--url http://127.0.0.1:8000/mcp", calls[check])
        self.assertLess(check, tunnel)

    def test_failed_tools_check_stops_mcp_without_starting_tunnel(self) -> None:
        self.install_units()
        result = self.run_script("up", FAIL_TOOLS="1")
        self.assertNotEqual(result.returncode, 0)
        calls = self.calls()
        self.assertIn("systemctl --user stop bee-dev-mcp.service", calls)
        self.assertNotIn("systemctl --user start bee-dev-mcp-tunnel.service", calls)

    def test_doctor_keeps_tools_check_output_and_fails(self) -> None:
        self.install_units()
        result = self.run_script("doctor", FAIL_TOOLS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MCP tool contract check failed", result.stderr)
        self.assertTrue(any(call.startswith("python ") for call in self.calls()))

    def test_status_propagates_service_failure(self) -> None:
        result = self.run_script("status", FAIL_MCP_STATUS="1")
        self.assertNotEqual(result.returncode, 0)

    def test_status_propagates_health_failure(self) -> None:
        result = self.run_script("status", FAIL_HEALTH="1")
        self.assertNotEqual(result.returncode, 0)

    def test_status_propagates_readiness_failure(self) -> None:
        result = self.run_script("status", FAIL_READY="1")
        self.assertNotEqual(result.returncode, 0)

    def test_stop_stops_tunnel_then_mcp_exactly(self) -> None:
        result = self.run_script("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [
            "systemctl --user stop bee-dev-mcp-tunnel.service",
            "systemctl --user stop bee-dev-mcp.service",
        ])

    def test_logs_is_one_combined_follow(self) -> None:
        result = self.run_script("logs")
        self.assertEqual(result.returncode, 0, result.stderr)
        journal = [call for call in self.calls() if call.startswith("journalctl ")]
        self.assertEqual(journal, [
            "journalctl --user -u bee-dev-mcp.service -u bee-dev-mcp-tunnel.service -f"
        ])
        self.assertNotIn("--no-pager", journal[0])

    def test_up_outputs_versionz_and_refresh_warning(self) -> None:
        self.install_units()
        result = self.run_script("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ChatGPT connector schema is not verified by local doctor.", result.stdout)
        self.assertIn("Stack is up", result.stdout)
        calls = self.calls()
        self.assertTrue(any("versionz" in call for call in calls))

    def test_restart_outputs_versionz_and_refresh_warning(self) -> None:
        self.install_units()
        result = self.run_script("restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ChatGPT connector schema is not verified by local doctor.", result.stdout)
        self.assertIn("Stack restarted", result.stdout)
        calls = self.calls()
        self.assertTrue(any("versionz" in call for call in calls))

    def test_doctor_reports_tool_contract_and_chatgpt_not_verified(self) -> None:
        self.install_units()
        result = self.run_script("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Local MCP health: OK", result.stdout)
        self.assertIn("Local MCP readiness: OK", result.stdout)
        self.assertIn("Local MCP tool contract: OK", result.stdout)
        self.assertIn("Tunnel service: active", result.stdout)
        self.assertIn("ChatGPT connector schema: NOT VERIFIED", result.stdout)
        self.assertIn("Doctor: all checks passed", result.stdout)

    def test_unchecked_chatgpt_schema_alone_not_failure(self) -> None:
        self.install_units()
        result = self.run_script("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("problem", result.stderr)

    def test_doctor_hides_transport_exception_details(self) -> None:
        self.install_units()
        result = self.run_script("doctor", FAIL_TOOLS="1")
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertNotIn("MCP transport or protocol check failed", output)
        self.assertNotIn("Traceback", output)
        self.assertNotIn("HTTP", output)

    def test_doctor_reports_closed_list_without_secret_values_or_mutation(self) -> None:
        self.install_units()
        result = self.run_script(
            "doctor", FAIL_MANAGER="1", FAIL_MCP_ACTIVE="1", FAIL_HEALTH="1"
        )
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn("systemd user manager unavailable", output)
        self.assertIn("service inactive: bee-dev-mcp.service", output)
        self.assertIn("health endpoint unavailable", output)
        self.assertNotIn("also-never-print-this", output)
        calls = self.calls()
        self.assertFalse(any(" start " in f" {call} " or " stop " in f" {call} " for call in calls))


class StaticAcceptanceTests(unittest.TestCase):
    def test_start_script_uses_no_legacy_privilege_or_background_mechanisms(self) -> None:
        source = (ROOT / "start.sh").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"\bsudo\b")
        self.assertNotRegex(source, r"\bnohup\b")
        self.assertNotRegex(source, r"(?i)(?:\bpid[_-]?file\b|\.pid\b)")
        self.assertNotRegex(source, r"(?<![&>])&(?![&>])")

    def test_units_match_safety_contract(self) -> None:
        mcp = (ROOT / "ops/systemd/bee-dev-mcp.service").read_text(encoding="utf-8")
        tunnel = (ROOT / "ops/systemd/bee-dev-mcp-tunnel.service").read_text(encoding="utf-8")
        self.assertIn("Type=simple", mcp)
        self.assertIn("WorkingDirectory=/home/bee/Pro/bee-dev-mcp", mcp)
        self.assertIn("/.venv/bin/python", mcp)
        self.assertIn("server.py", mcp)
        self.assertNotIn("uv ", mcp)
        self.assertNotIn("[Install]", mcp + tunnel)
        self.assertNotIn("secrets.env", mcp)
        self.assertIn("After=bee-dev-mcp.service", tunnel)
        self.assertIn("Requires=bee-dev-mcp.service", tunnel)
        self.assertIn("tunnel.env", tunnel)
        self.assertIn("tunnel-client run --profile bee-dev-mcp-test", tunnel)
        self.assertNotIn("server.py", tunnel)

    def test_health_routes_use_fastmcp_runtime_and_mcp_is_registered(self) -> None:
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn('@mcp.custom_route("/healthz", methods=["GET"])', source)
        self.assertIn('@mcp.custom_route("/readyz", methods=["GET"])', source)
        self.assertIn('PlainTextResponse("live")', source)
        self.assertIn('PlainTextResponse("ready")', source)
        self.assertIn('mcp.run(transport="streamable-http")', source)
        self.assertNotIn("StreamableHTTPTransport", source)
        self.assertNotIn("mcp._mcp_server", source)
        self.assertNotIn("uvicorn.run", source)
        if TMP_ROOT is None:
            self.skipTest("no writable temporary storage is available")
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temp:
            config = Path(temp) / "projects.yml"
            config.write_text(
                "limits:\n"
                "  max_file_bytes: 200000\n"
                "  max_diff_chars: 120000\n"
                "  max_context_chars: 160000\n"
                "  max_tree_entries: 4000\n"
                "  max_search_results: 200\n"
                "  max_review_page_chars: 30000\n"
                "deny_paths: []\n"
                "projects:\n"
                f"  test-proj:\n"
                f"    source: {ROOT}\n"
                f"    base_branch: main\n"
                f"    context_modes:\n"
                f"      A:\n"
                f"        files:\n"
                f"          - README.md\n",
                encoding="utf-8"
            )
            old_config = os.environ.get("BEE_MCP_CONFIG")
            os.environ["BEE_MCP_CONFIG"] = str(config)
            try:
                spec = importlib.util.spec_from_file_location(
                    "tested_server",
                    ROOT / "server.py",
                )
                if spec is None or spec.loader is None:
                    raise AssertionError("Cannot load server.py")

                loader = spec.loader
                module = importlib.util.module_from_spec(spec)
                loader.exec_module(module)
                app = module.mcp.streamable_http_app()
                routes = {route.path: route for route in app.routes}
                self.assertIn("/mcp", routes)
                for path, body in (("/healthz", b"live"), ("/readyz", b"ready")):
                    response = asyncio.run(routes[path].endpoint(None))
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.body, body)
                    self.assertEqual(response.media_type, "text/plain")
            finally:
                if old_config is None:
                    os.environ.pop("BEE_MCP_CONFIG", None)
                else:
                    os.environ["BEE_MCP_CONFIG"] = old_config


class CheckMcpToolsTests(unittest.TestCase):
    @staticmethod
    def _module():
        spec = importlib.util.spec_from_file_location(
            "check_mcp_tools", ROOT / "scripts/check_mcp_tools.py"
        )
        if spec is None or spec.loader is None:
            raise AssertionError("Cannot load check_mcp_tools.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_complete_contract(self) -> None:
        module = self._module()
        output = io.StringIO()
        with patch.object(module, "_list_tools", AsyncMock(
            return_value=set(module.EXPECTED_TOOLS)
        )), patch("sys.argv", ["check_mcp_tools.py", "--url", "http://test/mcp"]), \
                redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.assertEqual(module.main(), 0)
        lines = output.getvalue().splitlines()
        self.assertNotIn("missing MCP tool", lines[0])
        self.assertTrue(any(line.startswith("tool_contract_sha256=") for line in lines))

    def test_missing_tools_are_individual_and_deterministic(self) -> None:
        module = self._module()
        error = io.StringIO()
        found = set(module.EXPECTED_TOOLS) - {
            "get_review_manifest", "get_review_bundle_page"
        }
        with patch.object(module, "_list_tools", AsyncMock(return_value=found)), \
                patch("sys.argv", ["check_mcp_tools.py", "--url", "http://test/mcp"]), \
                redirect_stdout(io.StringIO()), redirect_stderr(error):
            self.assertEqual(module.main(), 1)
        self.assertEqual(error.getvalue().splitlines(), [
            "missing MCP tool: get_review_manifest",
            "missing MCP tool: get_review_bundle_page",
        ])

    def test_unexpected_tool_fails_contract(self) -> None:
        module = self._module()
        error = io.StringIO()
        found = set(module.EXPECTED_TOOLS) | {"write_project_file"}

        with patch.object(
            module,
            "_list_tools",
            AsyncMock(return_value=found),
        ), patch(
            "sys.argv",
            ["check_mcp_tools.py", "--url", "http://test/mcp"],
        ), redirect_stdout(
            io.StringIO()
        ), redirect_stderr(error):
            self.assertEqual(module.main(), 1)

        self.assertEqual(
            error.getvalue(),
            "unexpected MCP tool: write_project_file\n",
        )

    def test_transport_failure_is_brief(self) -> None:
        module = self._module()
        error = io.StringIO()
        with patch.object(module, "_list_tools", AsyncMock(
            side_effect=RuntimeError("token=secret")
        )), patch("sys.argv", ["check_mcp_tools.py", "--url", "http://test/mcp"]), \
                redirect_stdout(io.StringIO()), redirect_stderr(error):
            self.assertEqual(module.main(), 1)
        self.assertEqual(error.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
