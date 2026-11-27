from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
MCP_SERVICE = "beemcp.service"
TUNNEL_SERVICE = "beemcp-tunnel.service"
MCP_TEMPLATE = PROJECT_ROOT / "ops" / "systemd" / f"{MCP_SERVICE}.in"
TUNNEL_TEMPLATE = PROJECT_ROOT / "ops" / "systemd" / f"{TUNNEL_SERVICE}.in"
CHECKER = PROJECT_ROOT / "scripts" / "check_mcp_tools.py"
REQUIRED_ENV_VARS = (
    "BEEMCP_HOST",
    "BEEMCP_PORT",
    "BEEMCP_TUNNEL_PROFILE",
    "BEEMCP_TUNNEL_ID",
    "BEEMCP_TUNNEL_ADMIN_ADDR",
    "BEEMCP_CONTROL_PLANE_URL",
    "CONTROL_PLANE_API_KEY",
    "BEEMCP_GITHUB_TOKEN",
)
PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class LifecycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeSettings:
    host: str
    port: int
    profile: str
    tunnel_id: str
    tunnel_admin_addr: str
    control_plane_url: str
    api_key: str
    github_token: str = field(repr=False)
    config_home: Path

    @property
    def mcp_base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def healthz_url(self) -> str:
        return f"{self.mcp_base_url}/healthz"

    @property
    def readyz_url(self) -> str:
        return f"{self.mcp_base_url}/readyz"

    @property
    def versionz_url(self) -> str:
        return f"{self.mcp_base_url}/versionz"

    @property
    def mcp_url(self) -> str:
        return f"{self.mcp_base_url}/mcp"

    @property
    def tunnel_profile_path(self) -> Path:
        return self.config_home / "tunnel-client" / f"{self.profile}.yaml"

    @property
    def mcp_env_file(self) -> Path:
        return self.config_home / "beemcp" / "mcp.env"

    @property
    def systemd_dir(self) -> Path:
        return self.config_home / "systemd" / "user"


def command(
    args: list[str],
    *,
    check: bool = True,
    stdout: int | None = None,
    stderr: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        text=True,
        stdout=stdout,
        stderr=stderr,
    )


def require_command(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise LifecycleError(f"Required CLI not found: {name}")
    return found


def read_env() -> dict[str, str]:
    if not ENV_FILE.is_file():
        raise LifecycleError(f".env not found: {ENV_FILE}")
    values: dict[str, str] = {}
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise LifecycleError(f"Cannot read .env: {error}") from error
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in line:
            raise LifecycleError(f"Invalid .env line {number}")
        key, value = line.split("=", 1)
        if not key or key.strip() != key:
            raise LifecycleError(f"Invalid .env line {number}")
        values[key] = value
    return values


def load_settings() -> RuntimeSettings:
    values = read_env()
    missing = [name for name in REQUIRED_ENV_VARS if not values.get(name)]
    if missing:
        raise LifecycleError(
            f"Missing required variables in {ENV_FILE}: {', '.join(missing)}"
        )
    try:
        port = int(values["BEEMCP_PORT"])
    except ValueError as error:
        raise LifecycleError("BEEMCP_PORT must be an integer 1-65535") from error
    if not 1 <= port <= 65535:
        raise LifecycleError("BEEMCP_PORT must be an integer 1-65535")
    profile = values["BEEMCP_TUNNEL_PROFILE"]
    if PROFILE_RE.fullmatch(profile) is None:
        raise LifecycleError("BEEMCP_TUNNEL_PROFILE has an invalid format")
    for name in (
        "BEEMCP_HOST",
        "BEEMCP_PORT",
        "BEEMCP_TUNNEL_PROFILE",
        "BEEMCP_TUNNEL_ID",
        "BEEMCP_TUNNEL_ADMIN_ADDR",
        "BEEMCP_CONTROL_PLANE_URL",
    ):
        if any(character in values[name] for character in ("\0", "\n", "\r")):
            raise LifecycleError(f"{name} contains unsupported characters")
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    github_token = values["BEEMCP_GITHUB_TOKEN"]
    if any(
        character.isspace() or not character.isprintable() for character in github_token
    ):
        raise LifecycleError(
            "BEEMCP_GITHUB_TOKEN contains whitespace or control characters"
        )
    if len(github_token) < 10:
        raise LifecycleError("BEEMCP_GITHUB_TOKEN length is too short")
    return RuntimeSettings(
        host=values["BEEMCP_HOST"],
        port=port,
        profile=profile,
        tunnel_id=values["BEEMCP_TUNNEL_ID"],
        tunnel_admin_addr=values["BEEMCP_TUNNEL_ADMIN_ADDR"],
        control_plane_url=values["BEEMCP_CONTROL_PLANE_URL"],
        api_key=values["CONTROL_PLANE_API_KEY"],
        github_token=github_token,
        config_home=config_home,
    )


def atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".tmp.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        temporary.chmod(mode)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def generate_tunnel_profile(settings: RuntimeSettings) -> None:
    profile = {
        "config_version": 1,
        "control_plane": {
            "base_url": settings.control_plane_url,
            "tunnel_id": settings.tunnel_id,
            "api_key": "env:CONTROL_PLANE_API_KEY",
        },
        "health": {"listen_addr": settings.tunnel_admin_addr},
        "admin_ui": {"open_browser": False},
        "log": {"level": "info", "format": "json"},
        "mcp": {"server_urls": [{"channel": "main", "url": settings.mcp_url}]},
    }
    atomic_write(
        settings.tunnel_profile_path,
        yaml.safe_dump(profile, sort_keys=False),
        0o600,
    )


def generate_mcp_env_file(settings: RuntimeSettings) -> None:
    content = f"BEEMCP_GITHUB_TOKEN={settings.github_token}\n"
    atomic_write(settings.mcp_env_file, content, 0o600)


def systemd_escape(value: str) -> str:
    return value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')


def render_unit(
    template: Path, destination: Path, replacements: dict[str, str]
) -> None:
    try:
        content = template.read_text(encoding="utf-8")
    except OSError as error:
        raise LifecycleError(f"Source unit missing: {template.name}") from error
    for marker, value in replacements.items():
        content = content.replace(marker, systemd_escape(value))
    if re.search(r"@@[^@]+@@", content):
        raise LifecycleError(f"Unresolved placeholder in {template.name}")
    atomic_write(destination, content, 0o644)


def sync_runtime(settings: RuntimeSettings) -> None:
    if not MCP_TEMPLATE.is_file():
        raise LifecycleError(f"Source unit missing: {MCP_TEMPLATE.name}")
    if not TUNNEL_TEMPLATE.is_file():
        raise LifecycleError(f"Source unit missing: {TUNNEL_TEMPLATE.name}")
    tunnel_client = require_command("tunnel-client")
    generate_tunnel_profile(settings)
    generate_mcp_env_file(settings)
    render_unit(
        MCP_TEMPLATE,
        settings.systemd_dir / MCP_SERVICE,
        {
            "@@SCRIPT_DIR@@": str(PROJECT_ROOT),
            "@@VENV_PYTHON@@": str(VENV_PYTHON),
            "@@MCP_HOST@@": settings.host,
            "@@MCP_PORT@@": str(settings.port),
            "@@MCP_ENV_FILE@@": str(settings.mcp_env_file),
        },
    )
    render_unit(
        TUNNEL_TEMPLATE,
        settings.systemd_dir / TUNNEL_SERVICE,
        {
            "@@TUNNEL_CLIENT@@": tunnel_client,
            "@@TUNNEL_PROFILE@@": settings.profile,
            "@@ENV_FILE@@": str(ENV_FILE),
        },
    )
    command(["systemctl", "--user", "daemon-reload"])


def check_cli_tools() -> None:
    for name in ("systemctl", "journalctl", "curl", "tunnel-client"):
        require_command(name)


def service_stop(name: str) -> None:
    command(["systemctl", "--user", "stop", name], check=False)


def check_active(name: str) -> None:
    command(["systemctl", "--user", "is-active", name])


def wait_ready(settings: RuntimeSettings) -> None:
    for attempt in range(30):
        if (
            command(
                [
                    "curl",
                    "--fail",
                    "--silent",
                    "--output",
                    "/dev/null",
                    settings.readyz_url,
                ],
                check=False,
            ).returncode
            == 0
        ):
            return
        if attempt < 29:
            time.sleep(1)
    raise LifecycleError("MCP readiness check failed")


def check_tools(settings: RuntimeSettings, quiet: bool = False) -> bool:
    result = command(
        [str(VENV_PYTHON), "-B", str(CHECKER), "--url", settings.mcp_url],
        check=False,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
    )
    return result.returncode == 0


def reconciliation(settings: RuntimeSettings, success_message: str) -> None:
    service_stop(TUNNEL_SERVICE)
    service_stop(MCP_SERVICE)
    started = True
    try:
        sync_runtime(settings)
        command(["systemctl", "--user", "start", MCP_SERVICE])
        try:
            wait_ready(settings)
        except LifecycleError:
            command(
                ["journalctl", "--user", "-u", MCP_SERVICE, "--no-pager", "-n", "40"],
                check=False,
            )
            raise
        if not check_tools(settings):
            raise LifecycleError("MCP tool contract check failed")
        command(["systemctl", "--user", "start", TUNNEL_SERVICE])
        check_active(MCP_SERVICE)
        check_active(TUNNEL_SERVICE)
        command(["curl", "--fail", "--silent", settings.versionz_url])
        print()
        print("ChatGPT connector schema is not verified by local doctor.")
        print(
            "After tool schema changes, refresh the BeeMCP app actions "
            "and start a new chat."
        )
        print(success_message)
    except LifecycleError, subprocess.CalledProcessError:
        if started:
            service_stop(TUNNEL_SERVICE)
            service_stop(MCP_SERVICE)
        raise


def cmd_setup() -> None:
    settings = load_settings()
    check_cli_tools()
    if not VENV_PYTHON.is_file() or not os.access(VENV_PYTHON, os.X_OK):
        raise LifecycleError("Virtualenv Python not found. Run ./start.sh setup again.")
    sync_runtime(settings)
    print("Setup complete")


def cmd_up() -> None:
    settings = load_settings()
    check_cli_tools()
    reconciliation(settings, "Stack is up")


def cmd_restart() -> None:
    settings = load_settings()
    check_cli_tools()
    reconciliation(settings, "Stack restarted")


def cmd_status() -> None:
    settings = load_settings()
    command(["systemctl", "--user", "status", MCP_SERVICE])
    command(["systemctl", "--user", "status", TUNNEL_SERVICE])
    command(
        ["curl", "--fail", "--silent", "--output", "/dev/null", settings.healthz_url]
    )
    command(
        ["curl", "--fail", "--silent", "--output", "/dev/null", settings.readyz_url]
    )


def cmd_logs(arguments: list[str]) -> None:
    command(
        [
            "journalctl",
            "--user",
            "-u",
            MCP_SERVICE,
            "-u",
            TUNNEL_SERVICE,
            "-f",
            *arguments,
        ]
    )


def cmd_stop() -> None:
    service_stop(TUNNEL_SERVICE)
    service_stop(MCP_SERVICE)
    print("Stack stopped")


def env_permissions_ok() -> tuple[bool, str]:
    try:
        mode = stat.S_IMODE(ENV_FILE.stat().st_mode)
    except OSError:
        return False, "cannot determine .env permissions"
    if mode & 0o177 or mode & 0o7000:
        return False, f".env permissions {mode:03o} (must be 0600 or stricter)"
    return True, ""


def main_profile_url(path: Path) -> str | None:
    try:
        profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError, yaml.YAMLError:
        return None
    if not isinstance(profile, dict):
        return None
    mcp = profile.get("mcp")
    if not isinstance(mcp, dict):
        return None
    servers = mcp.get("server_urls")
    if not isinstance(servers, list):
        return None
    for server in servers:
        if isinstance(server, dict) and server.get("channel") == "main":
            url = server.get("url")
            return url if isinstance(url, str) else None
    return None


def cmd_doctor() -> None:
    problems: list[str] = []
    lines: list[str] = []
    available_commands = {
        name: shutil.which(name) is not None
        for name in ("systemctl", "journalctl", "curl", "tunnel-client")
    }
    if not ENV_EXAMPLE.is_file():
        problems.append(".env.example missing")
    if not ENV_FILE.is_file():
        problems.append(".env file missing")
        settings = None
    else:
        permissions_ok, permission_problem = env_permissions_ok()
        if not permissions_ok:
            problems.append(permission_problem)
        try:
            settings = load_settings()
        except LifecycleError:
            settings = None
            problems.append(".env failed to load or validate")
    for name, available in available_commands.items():
        if not available:
            problems.append(f"required CLI missing: {name}")
    if not VENV_PYTHON.is_file() or not os.access(VENV_PYTHON, os.X_OK):
        problems.append("virtualenv python missing")
    if not MCP_TEMPLATE.is_file():
        problems.append(f"source unit missing: {MCP_SERVICE}.in")
    if not TUNNEL_TEMPLATE.is_file():
        problems.append(f"source unit missing: {TUNNEL_SERVICE}.in")
    if settings is not None:
        for unit in (MCP_SERVICE, TUNNEL_SERVICE):
            if not (settings.systemd_dir / unit).is_file():
                problems.append(f"installed unit missing: {unit}")
    if available_commands["systemctl"]:
        if command(
            ["systemctl", "--user", "show-environment"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode:
            problems.append("systemd user manager unavailable")
        for service, label in ((MCP_SERVICE, "MCP"), (TUNNEL_SERVICE, "Tunnel")):
            if command(
                ["systemctl", "--user", "is-active", service], check=False
            ).returncode:
                problems.append(f"service inactive: {service}")
            else:
                lines.append(f"{label} service: active")
    if settings is not None:
        profile_url = main_profile_url(settings.tunnel_profile_path)
        if not settings.tunnel_profile_path.is_file():
            problems.append(
                f"tunnel profile missing: {settings.tunnel_profile_path.name}"
            )
        elif profile_url is None:
            problems.append("tunnel profile main MCP URL missing")
        elif profile_url != settings.mcp_url:
            problems.extend(
                (
                    "Tunnel MCP URL mismatch:",
                    f" configured: {profile_url}",
                    f" expected:   {settings.mcp_url}",
                )
            )
        if not settings.mcp_env_file.is_file():
            problems.append(f"MCP env file missing: {settings.mcp_env_file}")
        else:
            try:
                mcp_env_mode = stat.S_IMODE(settings.mcp_env_file.stat().st_mode)
                if mcp_env_mode & 0o177 or mcp_env_mode & 0o7000:
                    problems.append(
                        f"MCP env file permissions {mcp_env_mode:03o} (must be 0600)"
                    )
                mcp_env_content = settings.mcp_env_file.read_text(encoding="utf-8")
                expected_mcp_env_content = (
                    f"BEEMCP_GITHUB_TOKEN={settings.github_token}\n"
                )
                if mcp_env_content != expected_mcp_env_content:
                    problems.append("MCP env file has unexpected content")
            except OSError:
                problems.append("MCP env file cannot be read")
        mcp_unit_path = settings.systemd_dir / MCP_SERVICE
        if mcp_unit_path.is_file():
            mcp_unit_text = mcp_unit_path.read_text(encoding="utf-8")
            if str(settings.mcp_env_file) not in mcp_unit_text:
                problems.append("MCP unit does not reference the MCP env file")
        if available_commands["curl"]:
            if command(
                [
                    "curl",
                    "--fail",
                    "--silent",
                    "--output",
                    "/dev/null",
                    settings.healthz_url,
                ],
                check=False,
            ).returncode:
                problems.append("health endpoint unavailable")
            else:
                lines.append("Local MCP health: OK")
            if command(
                [
                    "curl",
                    "--fail",
                    "--silent",
                    "--output",
                    "/dev/null",
                    settings.readyz_url,
                ],
                check=False,
            ).returncode:
                problems.append("readiness endpoint unavailable")
            else:
                lines.append("Local MCP readiness: OK")
        if VENV_PYTHON.is_file() and os.access(VENV_PYTHON, os.X_OK):
            if check_tools(settings, quiet=True):
                lines.append("Local MCP tool contract: OK")
            else:
                problems.append("MCP tool contract check failed")
    for line in lines:
        print(line)
    if problems:
        print()
        print(f"Doctor found {len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f" - {problem}", file=sys.stderr)
        raise LifecycleError("")
    print(
        "ChatGPT connector schema: NOT VERIFIED — refresh app actions after "
        "schema changes"
    )
    print("Doctor: all checks passed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", nargs="?", default="up")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    commands = {
        "setup": cmd_setup,
        "up": cmd_up,
        "restart": cmd_restart,
        "status": cmd_status,
        "stop": cmd_stop,
        "doctor": cmd_doctor,
    }
    try:
        if arguments.command == "logs":
            cmd_logs(arguments.arguments)
        elif arguments.command in commands:
            commands[arguments.command]()
        else:
            raise LifecycleError(
                "Usage: start.py {setup|up|status|logs|restart|stop|doctor}"
            )
    except LifecycleError as error:
        if str(error):
            print(error, file=sys.stderr)
        return 1
    except subprocess.CalledProcessError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
