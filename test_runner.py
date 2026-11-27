"""Run a fixed set of local pytest suites inside Bubblewrap and a bounded user scope."""

from __future__ import annotations

import ctypes
import errno
import fnmatch
import hashlib
import os
import re
import selectors
import signal
import stat
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import ExitStack
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Callable

WORKER = Path(__file__).with_name("test_worker.py")
GIT = "/usr/bin/git"
BWRAP = "/usr/bin/bwrap"
SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEMCTL = "/usr/bin/systemctl"
MAX_TIMEOUT = 180
MAX_FILES = 2048
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_HISTORY = 32
SOURCE_DIRS = {"src", "tests", "config", "ops", "scripts"}
RESERVED_DIRS = {
    "bin",
    "dev",
    "lib",
    "lib64",
    "proc",
    "pytest-deps",
    "sys",
    "tmp",
    "usr",
}
SOURCE_EXTENSIONS = {
    ".css",
    ".html",
    ".in",
    ".js",
    ".json",
    ".md",
    ".png",
    ".py",
    ".sh",
    ".svg",
    ".toml",
    ".txt",
    ".typed",
    ".webp",
    ".yaml",
    ".yml",
}
SAFE_ROOT_FILES = {".env.example", ".gitignore", "uv.lock"}
PRIVATE_PART = re.compile(
    r"(?:credentials?|secrets?|tokens?|private[-_]?keys?|id_(?:rsa|ed25519)|"
    r"logs?|storage|state|cache|node_modules|__pycache__)(?:[.-].*)?\Z",
    re.I,
)
STATUS_LINE = re.compile(
    rb"BEEMCP_RESULT ([0-9]{1,6}) ([0-9]{1,6}) ([0-9]{1,6}) ([0-9]{1,8})\Z"
)
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


@dataclass(frozen=True)
class Suite:
    target: str
    tests: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    loopback: bool = False
    pytest_provider: str | None = None


SUITES = {
    "beeagent_conversation_web": Suite(
        "beeagent",
        (
            "tests/test_beescan_conversation.py::test_secret_redaction_is_operator_bound_and_covers_cookie_values",
            "tests/test_beescan_conversation.py::test_send_missing_scope_never_calls_target",
            "tests/test_beescan_web.py::test_workspace_tab_allowlist_is_readonly",
            "tests/test_beescan_web.py::test_workspace_results_are_readonly_and_localized",
        ),
        ("beeui", "beescan"),
    ),
    "beeui_unit": Suite(
        "beeui",
        ("tests/test_blocks.py", "tests/test_locale.py", "tests/test_navigation.py"),
    ),
    "beescan_unit": Suite("beescan", ("tests/test_catalog.py", "tests/test_domain.py")),
    "beemcp_unit": Suite(
        "beemcp",
        (
            "tests/test_github_context.py",
            "tests/test_local_artifacts.py",
            "tests/test_read_only_tools.py",
            "tests/test_review_sources.py",
            "tests/test_start_script.py",
            "tests/test_test_runner.py",
        ),
        loopback=True,
        pytest_provider="beeui",
    ),
}
LIMITS = {
    "cpu_percent": (100, 200),
    "memory_mib": (768, 2048),
    "processes": (64, 128),
    "file_mib": (16, 32),
    "temp_mib": (64, 128),
    "open_files": (256, 512),
    "output_bytes": (16384, 65536),
}


def validate_config(value: Any, projects: dict[str, Any]) -> dict[str, Any]:
    if value is None:
        return {"enabled": False}
    if not isinstance(value, dict) or set(value) - {"enabled", "suites", "limits"}:
        raise RuntimeError("invalid test_runner settings")
    if type(value.get("enabled", False)) is not bool:
        raise RuntimeError("test_runner.enabled must be a boolean")
    suites = value.get("suites", {})
    limits = value.get("limits", {})
    if (
        not isinstance(suites, dict)
        or not isinstance(limits, dict)
        or set(limits) - LIMITS.keys()
    ):
        raise RuntimeError("invalid test_runner suites or limits")
    for name, entry in suites.items():
        registered = SUITES.get(name)
        if (
            registered is None
            or not isinstance(entry, dict)
            or set(entry) != {"target", "timeout_seconds"}
            or entry["target"] != registered.target
            or registered.target not in projects
            or any(dep not in projects for dep in registered.dependencies)
            or (
                registered.pytest_provider
                and registered.pytest_provider not in projects
            )
            or type(entry["timeout_seconds"]) is not int
            or not 1 <= entry["timeout_seconds"] <= MAX_TIMEOUT
        ):
            raise RuntimeError("invalid test_runner suite allowlist entry")
    for name, setting in limits.items():
        if type(setting) is not int or not 1 <= setting <= LIMITS[name][1]:
            raise RuntimeError(f"invalid test_runner limit {name}")
    return {
        "enabled": value.get("enabled", False),
        "suites": suites,
        "limits": {
            name: limits.get(name, default) for name, (default, _) in LIMITS.items()
        },
    }


class Refused(Exception):
    pass


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _capture(
    process: subprocess.Popen[bytes], limit: int, deadline: float
) -> tuple[bytes, bool]:
    """Drain the pipe with a deadline, retaining only a bounded tail."""
    assert process.stdout is not None
    tail = bytearray()
    size = 0
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.select(_remaining(deadline)):
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                break
            size += len(chunk)
            tail.extend(chunk)
            del tail[: max(len(tail) - limit, 0)]
        else:
            raise TimeoutError
    process.wait(timeout=_remaining(deadline))
    return bytes(tail), size > limit


def _fd(stack: ExitStack, data: bytes) -> int:
    # The uv standalone build of CPython may omit os.memfd_create.
    try:
        memfd_create = ctypes.CDLL(None, use_errno=True).memfd_create
    except AttributeError:
        raise Refused("unsupported_sandbox_requirement") from None
    memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    memfd_create.restype = ctypes.c_int
    fd = memfd_create(b"beemcp-test", 1)  # MFD_CLOEXEC
    if fd < 0:
        raise OSError(ctypes.get_errno(), "memfd_create")
    stack.callback(os.close, fd)
    with os.fdopen(os.dup(fd), "wb") as handle:
        handle.write(data)
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


def _open_snapshot_file(root_fd: int, parts: tuple[str, ...]) -> int:
    with ExitStack() as stack:
        directory = root_fd
        for part in parts[:-1]:
            directory = os.open(part, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            stack.callback(os.close, directory)
        return os.open(
            parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory
        )


def _source_path(value: str, deny: tuple[str, ...], sources_only: bool) -> bool:
    path = Path(value)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or value != path.as_posix()
    ):
        return False
    if sources_only and path.parts[0] != "src":
        return False
    if value in SAFE_ROOT_FILES:
        return True
    if any(part.startswith(".") or PRIVATE_PART.fullmatch(part) for part in path.parts):
        return False
    if any(fnmatch.fnmatchcase(value, pattern) for pattern in deny):
        return False
    if len(path.parts) == 1:
        return path.suffix in {".md", ".py", ".sh", ".toml", ".yml", ".yaml"}
    return path.parts[0] in SOURCE_DIRS and path.suffix in SOURCE_EXTENSIONS


def _snapshot(
    roots: dict[str, Path],
    target: str,
    deny: tuple[str, ...],
    stack: ExitStack,
    deadline: float,
) -> tuple[str, list[str], list[int], set[str]]:
    digest = hashlib.sha256()
    args, files = [], []
    present = set()
    total = 0
    for name, root in sorted(roots.items()):
        root_fd = os.open(root, os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, root_fd)
        paths = ["--", "src"] if name != target else []
        with subprocess.Popen(
            [
                GIT,
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
                *paths,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**ENV, "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1"},
        ) as git:
            try:
                listing, truncated = _capture(git, 512 * 1024, deadline)
            finally:
                if git.poll() is None:
                    git.kill()
            if truncated or git.returncode or (listing and not listing.endswith(b"\0")):
                raise Refused("snapshot_inventory_unavailable")
        for item in sorted(set(listing.split(b"\0")) - {b""}):
            _remaining(deadline)
            try:
                value = item.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if not _source_path(value, deny, name != target):
                continue
            try:
                fd = _open_snapshot_file(root_fd, Path(value).parts)
            except FileNotFoundError, NotADirectoryError:
                continue
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    continue
                raise
            with os.fdopen(fd, "rb") as source:
                actual = os.fstat(source.fileno())
                if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1:
                    continue
                if actual.st_size > MAX_FILE_BYTES or len(files) >= MAX_FILES:
                    raise Refused("snapshot_limit_exceeded")
                content = source.read(MAX_FILE_BYTES + 1)
                total += len(content)
                if len(content) > MAX_FILE_BYTES or total > MAX_SNAPSHOT_BYTES:
                    raise Refused("snapshot_limit_exceeded")
                after = os.fstat(source.fileno())
                if (after.st_ctime_ns, after.st_size) != (
                    actual.st_ctime_ns,
                    actual.st_size,
                ):
                    raise Refused("snapshot_changed_during_capture")
            mode = "0755" if actual.st_mode & 0o111 else "0644"
            digest.update(name.encode() + b"\0" + item + b"\0" + mode.encode())
            digest.update(hashlib.sha256(content).digest())
            snapshot_file = _fd(stack, content)
            files.append(snapshot_file)
            args.extend(
                ("--perms", mode, "--file", str(snapshot_file), str(root / value))
            )
            if name == target:
                present.add(value)
    return digest.hexdigest(), args, files, present


def _seccomp(stack: ExitStack) -> int:
    """Keep networking in the isolated network namespace, including socket creation."""
    lib = ctypes.CDLL("libseccomp.so.2")

    class Comparison(ctypes.Structure):
        _fields_ = [
            ("arg", ctypes.c_uint),
            ("op", ctypes.c_int),
            ("a", ctypes.c_uint64),
            ("b", ctypes.c_uint64),
        ]

    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(Comparison),
    ]
    lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    filt = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not filt:
        raise Refused("unsupported_sandbox_requirement")
    stack.callback(lib.seccomp_release, filt)
    # Allow only Linux AF_UNIX (1), AF_INET (2), and AF_INET6 (10).
    less, equal, greater = 2, 4, 6  # libseccomp SCMP_CMP_* values
    comparisons = [
        Comparison(0, less, 1, 0),
        *(Comparison(0, equal, value, 0) for value in range(3, 10)),
        Comparison(0, greater, 10, 0),
    ]
    for name in (b"socket", b"socketpair"):
        syscall = lib.seccomp_syscall_resolve_name(name)
        for comparison in comparisons:
            if lib.seccomp_rule_add_array(
                filt, 0x00050001, syscall, 1, ctypes.byref(comparison)
            ):
                raise Refused("unsupported_sandbox_requirement")
    for name in (
        b"socketcall",
        b"io_uring_setup",
        b"io_uring_enter",
        b"io_uring_register",
        b"keyctl",
        b"add_key",
        b"request_key",
    ):
        syscall = lib.seccomp_syscall_resolve_name(name)
        if syscall >= 0 and lib.seccomp_rule_add_array(
            filt, 0x00050001, syscall, 0, None
        ):
            raise Refused("unsupported_sandbox_requirement")
    fd = _fd(stack, b"")
    if lib.seccomp_export_bpf(filt, fd):
        raise Refused("unsupported_sandbox_requirement")
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


def _trusted_mount(
    arguments: list[str], path: Path, destination: Path | None = None
) -> None:
    if not path.exists() or path.is_symlink():
        raise Refused("python_environment_unavailable")
    arguments.extend(("--ro-bind", str(path), str(destination or path)))


def _runtime(args: list[str], root: Path, provider: Path | None) -> tuple[Path, bool]:
    venv = root / ".venv"
    entry = venv / "bin/python"
    python = entry.resolve(strict=True)
    uv_root = Path.home() / ".local/share/uv/python"
    if python.parent != Path("/usr/bin"):
        if (
            not python.is_relative_to(uv_root)
            or len(python.relative_to(uv_root).parts) != 3
        ):
            raise Refused("python_environment_unavailable")
        install = python.parent.parent
        _trusted_mount(args, install / "lib")
    _trusted_mount(args, python)
    args.extend(("--symlink", str(python), str(entry)))
    _trusted_mount(args, venv / "pyvenv.cfg")
    site = venv / "lib/python3.14/site-packages"
    _trusted_mount(args, site)
    if (site / "pytest").is_dir():
        return entry, False
    if provider is None:
        raise Refused("python_environment_unavailable")
    # Mount only the already installed pytest runtime from the approved provider.
    other = provider / ".venv/lib/python3.14/site-packages"
    for package in (
        "pytest",
        "_pytest",
        "py.py",
        "pluggy",
        "iniconfig",
        "packaging",
        "pygments",
    ):
        _trusted_mount(args, other / package, Path("/pytest-deps") / package)
    return entry, True


def _manager_env() -> dict[str, str]:
    runtime = f"/run/user/{os.getuid()}"
    return {
        **ENV,
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
    }


def _stop_scope(unit: str, process: subprocess.Popen[bytes]) -> None:
    try:
        subprocess.run(
            [SYSTEMCTL, "--user", "stop", unit],
            env=_manager_env(),
            timeout=4,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        if process.poll() is None:
            # The scope also has its own RuntimeMaxSec if the manager is unreachable.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=4)


class LocalTestRunner:
    def __init__(
        self,
        settings: dict[str, Any],
        source: Callable[[str], Path],
        deny: tuple[str, ...],
    ) -> None:
        self.settings = settings
        self.source = source
        self.deny = deny
        self.lock = threading.Lock()
        self.active: str | None = None
        self.jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def start(self, target: str, suite: str) -> dict[str, Any]:
        if not self.settings["enabled"]:
            return {"state": "refused", "reason": "test_runner_disabled"}
        spec = SUITES.get(suite)
        if (
            spec is None
            or suite not in self.settings["suites"]
            or spec.target != target
        ):
            return {"state": "refused", "reason": "target_or_suite_not_allowed"}
        with self.lock:
            if self.active is not None:
                return {"state": "refused", "reason": "busy"}
            job_id = uuid.uuid4().hex
            status = {
                "job_id": job_id,
                "target": target,
                "suite": suite,
                "snapshot_id": uuid.uuid4().hex,
                "state": "queued",
                "exit_code": None,
                "duration_seconds": None,
                "output_truncated": False,
            }
            self.jobs[job_id] = status
            while len(self.jobs) > MAX_HISTORY:
                self.jobs.popitem(last=False)
            self.active = job_id
            try:
                threading.Thread(
                    target=self._run, args=(job_id, suite, spec), daemon=True
                ).start()
            except RuntimeError:
                self.active = None
                self.jobs.pop(job_id)
                return {"state": "refused", "reason": "sandbox_unavailable"}
            return {
                key: status[key]
                for key in ("job_id", "target", "suite", "snapshot_id", "state")
            }

    def result(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            return dict(
                self.jobs.get(job_id, {"state": "refused", "reason": "job_not_found"})
            )

    def _run(self, job_id: str, name: str, spec: Suite) -> None:
        started = time.monotonic()
        deadline = started + self.settings["suites"][name]["timeout_seconds"]
        final: dict[str, Any] = {"state": "refused", "reason": "sandbox_unavailable"}
        with self.lock:
            self.jobs[job_id]["state"] = "running"
        try:
            final = self._execute(job_id, spec, deadline)
        except TimeoutError, subprocess.TimeoutExpired:
            final = {"state": "timeout", "reason": "time_limit"}
        except Refused as error:
            final["reason"] = str(error)
        except OSError, RuntimeError, ValueError:
            pass
        finally:
            with self.lock:
                self.jobs[job_id].update(
                    final, duration_seconds=round(time.monotonic() - started, 3)
                )
                self.active = None

    def _execute(self, job_id: str, spec: Suite, deadline: float) -> dict[str, Any]:
        limits = self.settings["limits"]
        roots = {name: self.source(name) for name in (spec.target, *spec.dependencies)}
        provider = self.source(spec.pytest_provider) if spec.pytest_provider else None
        root = roots[spec.target]
        mount_roots = list(roots.values()) + ([provider] if provider else [])
        for path in mount_roots:
            if (
                not path.is_absolute()
                or len(path.parts) < 2
                or path.parts[1] in RESERVED_DIRS
            ):
                raise Refused("source_path_overlaps_sandbox")
        if any(
            a.is_relative_to(b) or b.is_relative_to(a)
            for a, b in combinations(mount_roots, 2)
        ):
            raise Refused("source_path_overlaps_sandbox")
        with ExitStack() as stack:
            snapshot, filespec, fds, present = _snapshot(
                roots, spec.target, self.deny, stack, deadline
            )
            if any(test.split("::", 1)[0] not in present for test in spec.tests):
                raise Refused("suite_source_unavailable")
            with self.lock:
                self.jobs[job_id]["snapshot_sha256"] = snapshot
            seccomp = _seccomp(stack)
            worker = _fd(stack, WORKER.read_bytes())
            args = [
                "--unshare-all",
                "--unshare-user",
                "--disable-userns",
                "--die-with-parent",
                "--new-session",
                "--clearenv",
                "--cap-drop",
                "ALL",
                "--seccomp",
                str(seccomp),
                "--dev",
                "/dev",
                "--remount-ro",
                "/dev",
                "--proc",
                "/proc",
                "--remount-ro",
                "/proc",
                "--symlink",
                "usr/bin",
                "/bin",
                "--symlink",
                "usr/lib",
                "/lib",
                "--symlink",
                "usr/lib64",
                "/lib64",
                "--size",
                str(limits["temp_mib"] * 1024 * 1024),
                "--tmpfs",
                "/tmp",
                "--setenv",
                "HOME",
                "/tmp/home",
                "--setenv",
                "TMPDIR",
                "/tmp",
                "--setenv",
                "PATH",
                ENV["PATH"],
                "--setenv",
                "LC_ALL",
                ENV["LANG"],
                "--setenv",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
                "1",
                *filespec,
                "--file",
                str(worker),
                "/beemcp-worker.py",
            ]
            for fixed in ("/usr/lib", "/usr/lib64", "/usr/share/git-core"):
                _trusted_mount(args, Path(fixed))
            for binary in (
                "bash",
                "sh",
                "git",
                "env",
                "chmod",
                "cp",
                "mkdir",
                "rm",
                "sleep",
                "cat",
                "dirname",
                "grep",
                "head",
                "mktemp",
                "readlink",
                "sed",
                "touch",
            ):
                # Resolve only this explicit list of operating-system utilities.
                _trusted_mount(
                    args,
                    Path("/usr/bin", binary).resolve(strict=True),
                    Path("/usr/bin", binary),
                )
            python, fallback = _runtime(args, root, provider)
            args += ["--chdir", str(root), "--remount-ro", "/"]
            argv = _fd(stack, b"\0".join(os.fsencode(arg) for arg in args) + b"\0")
            unit = f"beemcp-test-{job_id}.scope"
            command = [
                SYSTEMD_RUN,
                "--user",
                "--scope",
                "--quiet",
                "--collect",
                f"--unit={unit}",
                "-p",
                f"CPUQuota={limits['cpu_percent']}%",
                "-p",
                f"MemoryMax={limits['memory_mib']}M",
                "-p",
                "MemorySwapMax=0",
                "-p",
                f"TasksMax={limits['processes']}",
                "-p",
                "KillMode=control-group",
                "-p",
                f"RuntimeMaxSec={_remaining(deadline):.3f}s",
                "-p",
                "TimeoutStopSec=1s",
                "--",
                BWRAP,
                "--args",
                str(argv),
                "--",
                str(python),
                "-IB",
                "/beemcp-worker.py",
                str(limits["open_files"]),
                str(limits["file_mib"] * 1024 * 1024),
                str(int(spec.loopback)),
                str(int(fallback)),
                *spec.tests,
            ]
            with subprocess.Popen(
                command,
                start_new_session=True,
                env=_manager_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                pass_fds=(*fds, seccomp, worker, argv),
            ) as process:
                timed_out = False
                output: bytes = b""
                truncated: bool = False
                try:
                    output, truncated = _capture(
                        process, limits["output_bytes"], deadline
                    )
                except TimeoutError, subprocess.TimeoutExpired:
                    timed_out = True
                finally:
                    _stop_scope(unit, process)
            if timed_out:
                return {
                    "state": "timeout",
                    "reason": "time_limit",
                    "exit_code": process.returncode,
                }
            return _report(process.returncode, output, truncated, spec.tests)


def _report(
    exit_code: int, output: bytes, truncated: bool, tests: tuple[str, ...]
) -> dict[str, Any]:
    result: dict[str, Any] = {"exit_code": exit_code, "output_truncated": truncated}
    lines = output.splitlines()
    if b"BEEMCP_UNSUPPORTED_SANDBOX" in lines:
        return {
            **result,
            "state": "refused",
            "reason": "unsupported_sandbox_requirement",
        }
    if b"BEEMCP_PYTHON_UNAVAILABLE" in lines:
        return {
            **result,
            "state": "refused",
            "reason": "python_environment_unavailable",
        }
    match = STATUS_LINE.fullmatch(lines[-1]) if lines else None
    if not match:
        if b"BEEMCP_TESTS_ENTERED" not in lines and not truncated:
            return {
                **result,
                "state": "refused",
                "reason": "unsupported_sandbox_requirement",
            }
        return {**result, "state": "failed", "reason": "test_process_interrupted"}
    passed, failed, skipped, failed_modules = map(int, match.groups())
    reasons = {
        1: "test_failures",
        2: "collection_interrupted",
        3: "pytest_internal_error",
        4: "pytest_usage_error",
        5: "no_tests_collected",
    }
    result.update(passed=passed, failed=failed, skipped=skipped)
    if exit_code == 0:
        return {**result, "state": "passed"}
    modules = tuple(dict.fromkeys(test.split("::", 1)[0] for test in tests))
    result["failure_modules"] = [
        module for index, module in enumerate(modules) if failed_modules & (1 << index)
    ]
    return {
        **result,
        "state": "failed",
        "reason": reasons.get(exit_code, "test_process_interrupted"),
    }
