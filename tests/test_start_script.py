from __future__ import annotations

import asyncio
import importlib.util
import io
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

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
            probe = Path(tempfile.mkdtemp(prefix="beemcp-probe-", dir=candidate))
        except OSError:
            continue
        probe.rmdir()
        return candidate
    return None


TMP_ROOT = writable_tmp_root()

ENV_CONTENT = """\
BEEMCP_HOST=127.0.0.1
BEEMCP_PORT=8765
BEEMCP_TUNNEL_PROFILE=beemcp
BEEMCP_TUNNEL_ID=test-tunnel
BEEMCP_TUNNEL_ADMIN_ADDR=127.0.0.1:8080
BEEMCP_CONTROL_PLANE_URL=https://api.openai.com
CONTROL_PLANE_API_KEY=sk-test-never-print
BEEMCP_GITHUB_TOKEN=github_test_token_placeholder
"""


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
        (self.repo / "config").mkdir()
        (self.repo / ".venv/bin").mkdir(parents=True)
        self.bin.mkdir()
        shutil.copy2(ROOT / "start.sh", self.repo / "start.sh")
        shutil.copy2(ROOT / "config/start.py", self.repo / "config/start.py")
        shutil.copy2(ROOT / ".env.example", self.repo / ".env.example")
        for name in ("beemcp.service.in", "beemcp-tunnel.service.in"):
            shutil.copy2(ROOT / "ops/systemd" / name, self.repo / "ops/systemd" / name)
        env_file = self.repo / ".env"
        env_file.write_text(ENV_CONTENT, encoding="utf-8")
        env_file.chmod(0o600)
        self.python = self.repo / ".venv/bin/python"
        self.python.write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = "{self.repo / "config/start.py"}" ]; then\n'
            f'  exec {shlex.quote(sys.executable)} "$@"\n'
            "fi\n"
            'echo "python $*" >> "$CALL_LOG"\n'
            'if [ "${FAIL_TOOLS:-0}" = 0 ]; then\n'
            '  echo "OK: all 9 expected tools found"\n'
            '  echo "tool_contract_sha256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"\n'
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        self.python.chmod(0o755)
        self._mock(
            "systemctl",
            r"""#!/bin/sh
echo "systemctl $*" >> "$CALL_LOG"
case " $* " in
  *" show-environment "*) [ "${FAIL_MANAGER:-0}" = 0 ] ;;
  *" is-active beemcp.service "*) [ "${FAIL_MCP_ACTIVE:-0}" = 0 ] ;;
  *" is-active beemcp-tunnel.service "*) [ "${FAIL_TUNNEL_ACTIVE:-0}" = 0 ] ;;
  *" status beemcp.service "*) [ "${FAIL_MCP_STATUS:-0}" = 0 ] ;;
  *" status beemcp-tunnel.service "*) [ "${FAIL_TUNNEL_STATUS:-0}" = 0 ] ;;
  *" start beemcp-tunnel.service "*) [ "${FAIL_TUNNEL_START:-0}" = 0 ] ;;
  *) exit 0 ;;
esac
""",
        )
        self._mock("journalctl", '#!/bin/sh\necho "journalctl $*" >> "$CALL_LOG"\n')
        self._mock(
            "uv",
            r"""#!/bin/sh
echo "uv cwd=$PWD $*" >> "$CALL_LOG"
mkdir -p .venv/bin
if [ ! -x .venv/bin/python ]; then
  printf '#!/bin/sh\nexit 0\n' > .venv/bin/python
  chmod +x .venv/bin/python
fi
""",
        )
        self._mock(
            "curl",
            r"""#!/bin/sh
echo "curl $*" >> "$CALL_LOG"
case "$*" in
  *readyz*) [ "${FAIL_READY:-0}" = 0 ] ;;
  *healthz*) [ "${FAIL_HEALTH:-0}" = 0 ] ;;
  *versionz*)
    [ "${FAIL_VERSIONZ:-0}" = 0 ] && \
      echo '{"service":"beemcp","git_head":"abc123","tool_contract_version":5,"tool_count":12,"tool_contract_sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}'
    [ "${FAIL_VERSIONZ:-0}" = 0 ]
    ;;
  *) exit 0 ;;
esac
""",
        )
        self._mock("tunnel-client", "#!/bin/sh\nexit 0\n")

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
        return (
            self.log.read_text(encoding="utf-8").splitlines()
            if self.log.exists()
            else []
        )

    def install_units(self) -> None:
        target = self.home / ".config/systemd/user"
        target.mkdir(parents=True, exist_ok=True)
        mcp_unit_content = (
            'Environment="BEEMCP_HOST=127.0.0.1"\n'
            f"EnvironmentFile={self.home / '.config/beemcp/mcp.env'}\n"
        )
        for name in ("beemcp.service", "beemcp-tunnel.service"):
            (target / name).write_text(
                mcp_unit_content if name == "beemcp.service" else "",
                encoding="utf-8",
            )
        profile_dir = self.home / ".config/tunnel-client"
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "beemcp.yaml").write_text(
            "mcp:\n  server_urls:\n    - channel: main\n"
            '      url: "http://127.0.0.1:8765/mcp"\n',
            encoding="utf-8",
        )
        mcp_env_dir = self.home / ".config/beemcp"
        mcp_env_dir.mkdir(parents=True, exist_ok=True)
        mcp_env_file = mcp_env_dir / "mcp.env"
        mcp_env_file.write_text(
            "BEEMCP_GITHUB_TOKEN=github_test_token_placeholder\n", encoding="utf-8"
        )
        mcp_env_file.chmod(0o600)

    def test_default_without_dotenv_bootstraps_then_validates_it(
        self,
    ) -> None:
        (self.repo / ".env").unlink()

        result = self.run_script()

        self.assertNotEqual(result.returncode, 0)

        env_file = self.repo / ".env"
        self.assertTrue(env_file.is_file())
        self.assertEqual(
            stat.S_IMODE(env_file.stat().st_mode),
            0o600,
        )

        output = result.stdout + result.stderr
        self.assertIn(
            "[init] .env not found, creating from .env.example",
            output,
        )
        self.assertIn("Missing required variables", output)
        self.assertIn("BEEMCP_TUNNEL_ID", output)
        self.assertIn("CONTROL_PLANE_API_KEY", output)
        self.assertNotIn("sk-test", output)

        calls = self.calls()
        self.assertIn(
            f"uv cwd={self.repo} sync --frozen",
            calls,
        )
        self.assertFalse(any(call.startswith("systemctl ") for call in calls))

    def test_setup_with_dotenv_does_idempotent_install(self) -> None:
        result = self.run_script("setup")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertIn(f"uv cwd={self.repo} sync --frozen", calls)
        self.assertIn("systemctl --user daemon-reload", calls)
        self.assertFalse(any(" start " in f" {call} " for call in calls))
        target = self.home / ".config/systemd/user"
        for name in ("beemcp.service", "beemcp-tunnel.service"):
            self.assertTrue((target / name).is_file())
        profile_path = self.home / ".config/tunnel-client/beemcp.yaml"
        self.assertTrue(profile_path.is_file())

    def test_setup_restricts_existing_dotenv_permissions(self) -> None:
        env_file = self.repo / ".env"
        env_file.chmod(0o644)

        result = self.run_script("setup")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)

    def test_repeated_setup_does_not_overwrite_dotenv(self) -> None:
        self.run_script("setup")
        content_before = (self.repo / ".env").read_text(encoding="utf-8")
        self.run_script("setup")
        content_after = (self.repo / ".env").read_text(encoding="utf-8")
        self.assertEqual(content_before, content_after)

    def test_missing_dotenv_variable_cannot_use_parent_environment(self) -> None:
        content = (self.repo / ".env").read_text(encoding="utf-8")
        content = "\n".join(
            line for line in content.splitlines() if not line.startswith("BEEMCP_PORT=")
        )
        (self.repo / ".env").write_text(
            content + "\n",
            encoding="utf-8",
        )

        result = self.run_script(
            "setup",
            BEEMCP_PORT="9999",
        )

        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn("Missing required variables", output)
        self.assertIn("BEEMCP_PORT", output)

    def test_profile_generated_from_env_with_custom_values(self) -> None:
        (self.repo / ".env").write_text(
            "BEEMCP_HOST=10.0.0.1\n"
            "BEEMCP_PORT=9999\n"
            "BEEMCP_TUNNEL_PROFILE=custom-profile\n"
            "BEEMCP_TUNNEL_ID=custom-tunnel\n"
            "BEEMCP_TUNNEL_ADMIN_ADDR=0.0.0.0:9090\n"
            "BEEMCP_CONTROL_PLANE_URL=https://custom.example.com\n"
            "CONTROL_PLANE_API_KEY=sk-custom\n"
            "BEEMCP_GITHUB_TOKEN=github_test_token_placeholder\n",
            encoding="utf-8",
        )
        self.run_script("setup")
        profile_path = self.home / ".config/tunnel-client/custom-profile.yaml"
        self.assertTrue(profile_path.is_file())
        content = profile_path.read_text(encoding="utf-8")
        self.assertIn("10.0.0.1:9999/mcp", content)
        self.assertIn("custom-tunnel", content)
        self.assertIn("0.0.0.0:9090", content)
        self.assertIn("https://custom.example.com", content)
        self.assertEqual(
            yaml.safe_load(content)["control_plane"]["api_key"],
            "env:CONTROL_PLANE_API_KEY",
        )
        self.assertNotIn("sk-custom", content)
        self.assertEqual(stat.S_IMODE(profile_path.stat().st_mode), 0o600)

    def test_port_change_propagates_to_checker_url_profile_and_unit(self) -> None:
        self.install_units()
        result = self.run_script("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        check = next(i for i, c in enumerate(calls) if c.startswith("python "))
        self.assertIn("--url http://127.0.0.1:8765/mcp", calls[check])

        (self.repo / ".env").write_text(
            "BEEMCP_HOST=127.0.0.1\n"
            "BEEMCP_PORT=9999\n"
            "BEEMCP_TUNNEL_PROFILE=beemcp\n"
            "BEEMCP_TUNNEL_ID=test-tunnel\n"
            "BEEMCP_TUNNEL_ADMIN_ADDR=127.0.0.1:8080\n"
            "BEEMCP_CONTROL_PLANE_URL=https://api.openai.com\n"
            "CONTROL_PLANE_API_KEY=sk-test\n"
            "BEEMCP_GITHUB_TOKEN=github_test_token_placeholder\n",
            encoding="utf-8",
        )
        self.log.unlink(missing_ok=True)
        result = self.run_script("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        check = next(i for i, c in enumerate(calls) if c.startswith("python "))
        self.assertIn("--url http://127.0.0.1:9999/mcp", calls[check])
        profile_path = self.home / ".config/tunnel-client/beemcp.yaml"
        self.assertIn("127.0.0.1:9999/mcp", profile_path.read_text(encoding="utf-8"))
        mcp_unit = self.home / ".config/systemd/user/beemcp.service"
        self.assertIn("BEEMCP_PORT=9999", mcp_unit.read_text(encoding="utf-8"))

    def test_source_templates_have_no_hardcoded_paths_or_secrets(self) -> None:
        for name in ("beemcp.service.in", "beemcp-tunnel.service.in"):
            content = (ROOT / "ops/systemd" / name).read_text(encoding="utf-8")
            self.assertNotIn("/home/bee", content)
            self.assertNotIn("beemcp-test", content)
            self.assertNotIn("127.0.0.1:8000", content)
            self.assertNotIn("127.0.0.1:8765", content)
            self.assertNotIn("sk-", content)
            self.assertNotIn("CONTROL_PLANE_API_KEY", content)

    def test_installed_mcp_unit_uses_repo_python_host_port_no_secrets(self) -> None:
        self.run_script("setup")
        unit = (self.home / ".config/systemd/user/beemcp.service").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"WorkingDirectory={self.repo}", unit)
        self.assertIn(
            f'ExecStart="{self.repo}/.venv/bin/python" "{self.repo}/server.py"', unit
        )
        self.assertIn("BEEMCP_HOST=127.0.0.1", unit)
        self.assertIn("BEEMCP_PORT=8765", unit)
        self.assertNotIn("CONTROL_PLANE_API_KEY", unit)
        self.assertNotIn(f"EnvironmentFile={self.repo / '.env'}", unit)
        self.assertIn("EnvironmentFile=", unit)
        self.assertIn("mcp.env", unit)

    def test_installed_tunnel_unit_uses_tunnel_client_and_dotenv(self) -> None:
        self.run_script("setup")
        unit = (self.home / ".config/systemd/user/beemcp-tunnel.service").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"EnvironmentFile={self.repo / '.env'}", unit)
        self.assertIn('run --profile "beemcp"', unit)
        self.assertIn("After=beemcp.service", unit)
        self.assertIn("Requires=beemcp.service", unit)
        self.assertNotIn("beemcp-test", unit)

    def test_up_and_restart_sync_profile_and_units_before_tunnel(self) -> None:
        self.install_units()
        result = self.run_script("up")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_tools_check_does_not_start_tunnel(self) -> None:
        self.install_units()
        result = self.run_script("up", FAIL_TOOLS="1")
        self.assertNotEqual(result.returncode, 0)
        calls = self.calls()
        self.assertIn("systemctl --user stop beemcp.service", calls)
        self.assertNotIn("systemctl --user start beemcp-tunnel.service", calls)

    def test_failed_tunnel_start_cleans_up_both_services(self) -> None:
        self.install_units()
        result = self.run_script("up", FAIL_TUNNEL_START="1")
        self.assertNotEqual(result.returncode, 0)
        calls = self.calls()
        self.assertIn("systemctl --user start beemcp-tunnel.service", calls)
        self.assertGreaterEqual(
            calls.count("systemctl --user stop beemcp-tunnel.service"),
            2,
        )
        self.assertGreaterEqual(
            calls.count("systemctl --user stop beemcp.service"),
            2,
        )

    def test_failed_readiness_does_not_start_tunnel_and_shows_log_tail(self) -> None:
        self.install_units()
        self._mock("sleep", "#!/bin/sh\nexit 0\n")
        result = self.run_script("up", FAIL_READY="1")
        self.assertNotEqual(result.returncode, 0)
        calls = self.calls()
        self.assertNotIn("systemctl --user start beemcp-tunnel.service", calls)
        self.assertIn("journalctl --user -u beemcp.service --no-pager -n 40", calls)

    def test_doctor_passes_when_everything_ok(self) -> None:
        self.install_units()
        result = self.run_script("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Local MCP health: OK", result.stdout)
        self.assertIn("Local MCP readiness: OK", result.stdout)
        self.assertIn("Local MCP tool contract: OK", result.stdout)
        self.assertIn("Tunnel service: active", result.stdout)
        self.assertIn("Doctor: all checks passed", result.stdout)

    def test_doctor_fails_on_profile_url_mismatch(self) -> None:
        self.install_units()
        profile_dir = self.home / ".config/tunnel-client"
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "beemcp.yaml").write_text(
            'mcp:\n  server_urls:\n    - channel: main\n      url: "http://127.0.0.1:8000/mcp"\n',
            encoding="utf-8",
        )
        result = self.run_script("doctor")
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn("Tunnel MCP URL mismatch", output)
        self.assertIn("configured: http://127.0.0.1:8000/mcp", output)
        self.assertIn("expected:   http://127.0.0.1:8765/mcp", output)
        self.assertNotIn("sk-test", output)
        self.assertNotIn("sk-", output)

    def test_doctor_restricts_existing_dotenv_permissions(self) -> None:
        self.install_units()
        env_file = self.repo / ".env"
        env_file.chmod(0o440)
        result = self.run_script("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            stat.S_IMODE(env_file.stat().st_mode),
            0o600,
        )

    def test_setup_rejects_escaping_tunnel_profile(self) -> None:
        content = (self.repo / ".env").read_text(encoding="utf-8")
        (self.repo / ".env").write_text(
            content.replace(
                "BEEMCP_TUNNEL_PROFILE=beemcp",
                "BEEMCP_TUNNEL_PROFILE=../../escape",
            ),
            encoding="utf-8",
        )
        result = self.run_script("setup")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BEEMCP_TUNNEL_PROFILE", result.stdout + result.stderr)

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
        self.assertIn("service inactive: beemcp.service", output)
        self.assertIn("health endpoint unavailable", output)
        self.assertNotIn("sk-test", output)
        calls = self.calls()
        self.assertFalse(
            any(" start " in f" {call} " or " stop " in f" {call} " for call in calls)
        )

    def test_restart_orders_stops_then_mcp_ready_tunnel_and_checks_both(self) -> None:
        self.install_units()
        result = self.run_script("restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        systemctl_calls = [call for call in calls if call.startswith("systemctl ")]
        self.assertEqual(
            systemctl_calls[0], "systemctl --user stop beemcp-tunnel.service"
        )
        self.assertEqual(systemctl_calls[1], "systemctl --user stop beemcp.service")
        # daemon-reload happens before start
        daemon_idx = next(i for i, c in enumerate(calls) if "daemon-reload" in c)
        start_idx = next(i for i, c in enumerate(calls) if "start beemcp.service" in c)
        self.assertLess(daemon_idx, start_idx)
        ready = next(i for i, call in enumerate(calls) if "readyz" in call)
        tunnel = calls.index("systemctl --user start beemcp-tunnel.service")
        self.assertLess(ready, tunnel)
        self.assertIn("systemctl --user is-active beemcp.service", calls)
        self.assertIn("systemctl --user is-active beemcp-tunnel.service", calls)

    def test_check_tools_uses_derived_url_and_precedes_tunnel(self) -> None:
        self.install_units()
        result = self.run_script("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        check = next(i for i, call in enumerate(calls) if call.startswith("python "))
        tunnel = calls.index("systemctl --user start beemcp-tunnel.service")
        self.assertIn("-B", calls[check])
        self.assertIn("--url http://127.0.0.1:8765/mcp", calls[check])
        self.assertLess(check, tunnel)

    # ── Status / stop / logs ────────────────────────────────────────
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
        self.assertEqual(
            self.calls(),
            [
                "systemctl --user stop beemcp-tunnel.service",
                "systemctl --user stop beemcp.service",
            ],
        )

    def test_logs_is_one_combined_follow(self) -> None:
        result = self.run_script("logs")
        self.assertEqual(result.returncode, 0, result.stderr)
        journal = [call for call in self.calls() if call.startswith("journalctl ")]
        self.assertEqual(
            journal,
            ["journalctl --user -u beemcp.service -u beemcp-tunnel.service -f"],
        )
        self.assertNotIn("--no-pager", journal[0])

    def test_up_outputs_versionz_and_refresh_warning(self) -> None:
        self.install_units()
        result = self.run_script("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "ChatGPT connector schema is not verified by local doctor.", result.stdout
        )
        self.assertIn("Stack is up", result.stdout)
        calls = self.calls()
        self.assertTrue(any("versionz" in call for call in calls))

    def test_restart_outputs_versionz_and_refresh_warning(self) -> None:
        self.install_units()
        result = self.run_script("restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "ChatGPT connector schema is not verified by local doctor.", result.stdout
        )
        self.assertIn("Stack restarted", result.stdout)
        calls = self.calls()
        self.assertTrue(any("versionz" in call for call in calls))

    def test_unchecked_chatgpt_schema_alone_not_failure(self) -> None:
        self.install_units()
        result = self.run_script("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("problem", result.stderr)

    # ── Paths with spaces ───────────────────────────────────────────
    def test_setup_works_with_spaces_in_path(self) -> None:
        self.assertTrue(" " in str(self.repo))
        result = self.run_script("setup")
        self.assertEqual(result.returncode, 0, result.stderr)


class StaticAcceptanceTests(unittest.TestCase):
    def test_start_script_uses_no_legacy_privilege_or_background_mechanisms(
        self,
    ) -> None:
        source = (ROOT / "start.sh").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"\bsudo\b")
        self.assertNotRegex(source, r"\bnohup\b")
        self.assertNotRegex(source, r"(?i)(?:\bpid[_-]?file\b|\.pid\b)")
        self.assertNotRegex(source.replace(" & ", ""), r"(?<![&>])&(?![&>])")

    def test_in_source_templates_have_no_hardcoded_user_paths(self) -> None:
        for name in ("beemcp.service.in", "beemcp-tunnel.service.in"):
            content = (ROOT / "ops/systemd" / name).read_text(encoding="utf-8")
            self.assertNotIn("/home/bee", content)
            self.assertNotIn("beemcp-test", content)
            self.assertNotIn("127.0.0.1:8000", content)
            self.assertNotIn("127.0.0.1:8765", content)
        mcp = (ROOT / "ops/systemd/beemcp.service.in").read_text(encoding="utf-8")
        self.assertIn("Type=simple", mcp)
        self.assertIn("server.py", mcp)
        self.assertNotIn("uv ", mcp)
        self.assertNotIn("[Install]", mcp)
        tunnel = (ROOT / "ops/systemd/beemcp-tunnel.service.in").read_text(
            encoding="utf-8"
        )
        self.assertIn("After=beemcp.service", tunnel)
        self.assertIn("Requires=beemcp.service", tunnel)
        self.assertNotIn("server.py", tunnel)
        self.assertNotIn("[Install]", tunnel + mcp)

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
            config_dir = Path(temp) / "config"
            config_dir.mkdir()
            config = config_dir / "projects.yml"
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
                encoding="utf-8",
            )
            old_config = os.environ.get("BEEMCP_CONFIG")
            old_host = os.environ.get("BEEMCP_HOST")
            old_port = os.environ.get("BEEMCP_PORT")
            old_token = os.environ.get("BEEMCP_GITHUB_TOKEN")
            os.environ["BEEMCP_CONFIG"] = str(config)
            os.environ["BEEMCP_HOST"] = "127.0.0.1"
            os.environ["BEEMCP_PORT"] = "8765"
            os.environ["BEEMCP_GITHUB_TOKEN"] = "github_test_token_placeholder"
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
                    os.environ.pop("BEEMCP_CONFIG", None)
                else:
                    os.environ["BEEMCP_CONFIG"] = old_config
                if old_host is None:
                    os.environ.pop("BEEMCP_HOST", None)
                else:
                    os.environ["BEEMCP_HOST"] = old_host
                if old_port is None:
                    os.environ.pop("BEEMCP_PORT", None)
                else:
                    os.environ["BEEMCP_PORT"] = old_port
                if old_token is None:
                    os.environ.pop("BEEMCP_GITHUB_TOKEN", None)
                else:
                    os.environ["BEEMCP_GITHUB_TOKEN"] = old_token


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
        with (
            patch.object(
                module,
                "_list_tools",
                AsyncMock(return_value=set(module.EXPECTED_TOOLS)),
            ),
            patch("sys.argv", ["check_mcp_tools.py", "--url", "http://test/mcp"]),
            redirect_stdout(output),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(module.main(), 0)
        lines = output.getvalue().splitlines()
        self.assertNotIn("missing MCP tool", lines[0])
        self.assertTrue(any(line.startswith("tool_contract_sha256=") for line in lines))

    def test_missing_tools_are_individual_and_deterministic(self) -> None:
        module = self._module()
        error = io.StringIO()
        found = set(module.EXPECTED_TOOLS) - {
            "get_review_manifest",
            "get_review_bundle_page",
        }
        with (
            patch.object(module, "_list_tools", AsyncMock(return_value=found)),
            patch("sys.argv", ["check_mcp_tools.py", "--url", "http://test/mcp"]),
            redirect_stdout(io.StringIO()),
            redirect_stderr(error),
        ):
            self.assertEqual(module.main(), 1)
        self.assertEqual(
            error.getvalue().splitlines(),
            [
                "missing MCP tool: get_review_manifest",
                "missing MCP tool: get_review_bundle_page",
            ],
        )

    def test_unexpected_tool_fails_contract(self) -> None:
        module = self._module()
        error = io.StringIO()
        found = set(module.EXPECTED_TOOLS) | {"write_project_file"}

        with (
            patch.object(
                module,
                "_list_tools",
                AsyncMock(return_value=found),
            ),
            patch(
                "sys.argv",
                ["check_mcp_tools.py", "--url", "http://test/mcp"],
            ),
            redirect_stdout(io.StringIO()),
            redirect_stderr(error),
        ):
            self.assertEqual(module.main(), 1)

        self.assertEqual(
            error.getvalue(),
            "unexpected MCP tool: write_project_file\n",
        )

    def test_transport_failure_is_brief(self) -> None:
        module = self._module()
        error = io.StringIO()
        with (
            patch.object(
                module,
                "_list_tools",
                AsyncMock(side_effect=RuntimeError("token=secret")),
            ),
            patch("sys.argv", ["check_mcp_tools.py", "--url", "http://test/mcp"]),
            redirect_stdout(io.StringIO()),
            redirect_stderr(error),
        ):
            self.assertEqual(module.main(), 1)
        self.assertEqual(error.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
