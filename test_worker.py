"""Trusted bootstrap invoked inside the restricted test process."""

import errno
import importlib
import os
import resource
import socket
import sys
from pathlib import Path


def check_network(loopback: bool) -> bool:
    # A fresh net namespace has only lo and cannot route these documentation addresses.
    # No application payload is sent.
    with open("/proc/net/dev") as devices:
        if {line.split(":", 1)[0].strip() for line in devices if ":" in line} != {"lo"}:
            return False
    for family, endpoint in (
        (socket.AF_INET, ("192.0.2.1", 443)),
        (socket.AF_INET6, ("2001:db8::1", 443)),
    ):
        with socket.socket(family, socket.SOCK_STREAM) as check:
            check.settimeout(0.2)
            if check.connect_ex(endpoint) != errno.ENETUNREACH:
                return False
    if loopback:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            with socket.create_connection(listener.getsockname(), timeout=0.2):
                connection, _ = listener.accept()
                connection.close()
    return True


def main() -> int:
    open_files, file_bytes, loopback, fallback, *tests = sys.argv[1:]
    resource.setrlimit(resource.RLIMIT_NOFILE, (int(open_files), int(open_files)))
    resource.setrlimit(resource.RLIMIT_FSIZE, (int(file_bytes), int(file_bytes)))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        if not all(
            os.statvfs(path).f_flag & os.ST_RDONLY for path in ("/", ".", sys.prefix)
        ):
            raise OSError
        if not check_network(loopback == "1"):
            raise OSError
    except OSError:
        print("BEEMCP_UNSUPPORTED_SANDBOX", flush=True)
        return 125
    Path("/tmp/home").mkdir(mode=0o700)
    if fallback == "1":
        sys.path.append("/pytest-deps")
    try:
        pytest_main = getattr(importlib.import_module("pytest"), "main")
    except ImportError, AttributeError:
        print("BEEMCP_PYTHON_UNAVAILABLE", flush=True)
        return 125

    class Summary:
        def __init__(self) -> None:
            self.passed = 0
            self.failed = 0
            self.skipped = 0
            self.failed_modules = 0
            self.modules = {
                name: index
                for index, name in enumerate(
                    dict.fromkeys(test.split("::", 1)[0] for test in tests)
                )
            }

        def record_failure(self, report) -> None:
            self.failed += 1
            module = self.modules.get(report.nodeid.split("::", 1)[0])
            if module is not None:
                self.failed_modules |= 1 << module

        def pytest_runtest_logreport(self, report) -> None:
            if report.failed:
                self.record_failure(report)
            elif report.skipped:
                self.skipped += 1
            elif report.when == "call" and report.passed:
                self.passed += 1

        def pytest_collectreport(self, report) -> None:
            if report.failed:
                self.record_failure(report)

    sys.path.insert(0, os.getcwd())
    summary = Summary()
    print("BEEMCP_TESTS_ENTERED", flush=True)
    code = pytest_main(
        ["-p", "no:cacheprovider", "--tb=no", "-q", "--basetemp=/tmp/pytest", *tests],
        plugins=[summary],
    )
    print(
        f"BEEMCP_RESULT {summary.passed} {summary.failed} {summary.skipped} {summary.failed_modules}",
        flush=True,
    )
    return int(code)


if __name__ == "__main__":
    sys.exit(main())
