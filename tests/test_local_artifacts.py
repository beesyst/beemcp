from __future__ import annotations

import asyncio
import codecs
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_read_only_tools as fixtures


class LocalArtifactTests(unittest.TestCase):
    _init_repo = fixtures.ReadOnlyToolsTests._init_repo
    _make_project_yml = fixtures.ReadOnlyToolsTests._make_project_yml
    _import_server = fixtures.ReadOnlyToolsTests._import_server

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="beemcp-artifacts-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self._init_repo("repo")
        self.config = self._make_project_yml(
            f"  artifacts:\n    source: {self.repo}\n    base_branch: main\n"
            "    context_modes:\n      A:\n        files: []\n"
        )
        self.section = "local_artifacts:\n  enabled: true\n  root: /tmp\n"
        self.mod = self._import_server(self.config + self.section)

    def write(self, name, content=b"report\n"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_requested_paths(self):
        for name in ("REPORT.md", "PROPOSED-BINDING.json", "runtime/evidence.json"):
            with self.subTest(name=name):
                path = self.write(name)
                result = self.mod.read_local_artifact(str(path))
                self.assertEqual(result["content"], "report\n")
                self.assertEqual(result["path"], str(path))
                self.assertEqual(
                    set(result),
                    {
                        "path",
                        "start_line",
                        "start_column",
                        "end_line",
                        "next_line",
                        "next_column",
                        "content",
                        "encoding",
                    },
                )

    def test_new_directory_without_registration(self):
        for prefix in (
            "anchor-",
            "codex-",
            "beedrill-",
            "jsp000089-direct-crossladder-",
            "project-x-",
            "arbitrary-",
            "unapproved-",
        ):
            with tempfile.TemporaryDirectory(prefix=prefix, dir="/tmp") as directory:
                path = Path(directory) / "REPORT.md"
                path.write_text("new report")
                self.assertEqual(
                    self.mod.read_local_artifact(str(path))["content"], "new report"
                )

    def test_file_directly_in_tmp(self):
        with tempfile.NamedTemporaryFile(suffix=".json", dir="/tmp") as artifact:
            artifact.write(b'{"report": true}\n')
            artifact.flush()
            result = self.mod.read_local_artifact(artifact.name)
            self.assertEqual(result["content"], '{"report": true}\n')
            self.assertEqual(Path(result["path"]).parent, Path("/tmp"))

    def test_plain_utf8_and_all_supported_extensions(self):
        for suffix in (".md", ".json", ".txt", ".log", ".csv", ".tsv"):
            with self.subTest(suffix=suffix):
                path = self.write("unicode" + suffix, "Привет 🌍\r\n".encode())
                result = self.mod.read_local_artifact(str(path))
                self.assertEqual(result["content"], "Привет 🌍\r\n")
                self.assertEqual(result["encoding"], "utf-8")

    def test_large_unicode_bom_crlf_long_line_incremental(self):
        content = "Привет 🌍\r\n" + "長" * 220000 + "\r\nend\r\n"
        path = self.write("large.md", codecs.BOM_UTF8 + content.encode())
        self.mod.LIMITS["max_context_chars"] = 4096
        line, column = 1, 0
        chunks = []
        with patch.object(
            Path, "read_bytes", side_effect=AssertionError("whole file read")
        ):
            for _ in range(100):
                result = self.mod.read_local_artifact(str(path), line, column)
                self.assertLessEqual(len(result["content"]), 4096)
                self.assertEqual(result["encoding"], "utf-8-sig")
                chunks.append(result["content"])
                if result["next_line"] is None:
                    break
                self.assertNotEqual(
                    (line, column), (result["next_line"], result["next_column"])
                )
                line, column = result["next_line"], result["next_column"]
            else:
                self.fail("pagination did not finish")
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), content)

    def test_utf_bom_decoding(self):
        for encoding in ("utf-16", "utf-32"):
            with self.subTest(encoding=encoding):
                path = self.write("bom.txt", "Привет\r\n".encode(encoding))
                self.assertEqual(
                    self.mod.read_local_artifact(str(path))["content"], "Привет\r\n"
                )

    def test_empty_file(self):
        result = self.mod.read_local_artifact(str(self.write("empty.csv", b"")))
        self.assertEqual(result["content"], "")
        self.assertIsNone(result["next_line"])

    def test_forbidden_paths(self):
        paths = [
            "/etc/passwd",
            "/home/bee/.ssh/id_ed25519",
            "/tmp-other/report.md",
            "/tmp/anchor-test/../../etc/passwd",
            "REPORT.md",
            str(self.root / "nul\x00.md"),
            "/tmp",
        ]
        for path in paths:
            with self.subTest(path=path), self.assertRaises(ValueError) as caught:
                self.mod.read_local_artifact(path)
            self.assertLess(len(str(caught.exception)), 100)

    def test_hidden_sensitive_extensions_and_deny_patterns(self):
        for name in (
            ".env",
            ".env.json",
            ".env.example",
            "secret.key",
            ".git/report.md",
            ".hidden/report.md",
            "credentials.json",
            "private-key.txt",
            "id_ed25519.txt",
            "runtime/credentials/report.md",
            "data.py",
            "__pycache__/report.md",
        ):
            path = self.write(name)
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.mod.read_local_artifact(str(path))
        path = self.write("custom-denied.md")
        self.mod.DENY_PATTERNS.append("**/custom-denied.md")
        with self.assertRaises(ValueError):
            self.mod.read_local_artifact(str(path))

    def test_symlinks_outside_root_and_between_arbitrary_directories(self):
        with tempfile.TemporaryDirectory(prefix="unapproved-", dir="/tmp") as other:
            target = Path(other) / "report.md"
            target.write_text("private")
            for index, destination in enumerate(
                (Path("/etc/passwd"), target, self.write("real.md"))
            ):
                link = self.root / f"link{index}.md"
                link.symlink_to(destination)
                with (
                    self.subTest(destination=destination),
                    self.assertRaises(ValueError),
                ):
                    self.mod.read_local_artifact(str(link))
            link_dir = self.root / "linked"
            link_dir.symlink_to(self.root, target_is_directory=True)
            with self.assertRaises(ValueError):
                self.mod.read_local_artifact(str(link_dir / "real.md"))

    def test_top_level_directory_symlink(self):
        link = self.root.with_name(self.root.name + "-link")
        link.symlink_to(self.root, target_is_directory=True)
        self.addCleanup(link.unlink)
        self.write("real.md")
        with self.assertRaises(ValueError):
            self.mod.read_local_artifact(str(link / "real.md"))

    def test_device_file_rejected_before_read(self):
        path = self.write("device.txt")
        real_fstat = os.fstat

        def device_stat(fd):
            metadata = real_fstat(fd)
            if stat.S_ISREG(metadata.st_mode):
                return SimpleNamespace(st_mode=stat.S_IFCHR, st_uid=metadata.st_uid)
            return metadata

        with patch.object(self.mod.os, "fstat", side_effect=device_stat):
            with self.assertRaises(ValueError):
                self.mod.read_local_artifact(str(path))

    def assert_foreign_owner_rejected(self, path, foreign_path):
        target = foreign_path.stat()
        real_fstat = os.fstat
        checked = []

        def foreign_stat(fd):
            metadata = real_fstat(fd)
            if (metadata.st_dev, metadata.st_ino) == (target.st_dev, target.st_ino):
                checked.append(fd)
                return SimpleNamespace(
                    st_mode=metadata.st_mode,
                    st_uid=os.geteuid() + 1,
                )
            return metadata

        with (
            patch.object(self.mod.os, "fstat", side_effect=foreign_stat),
            patch.object(self.mod, "_safe_read_file_chunked") as reader,
        ):
            with self.assertRaises(ValueError) as caught:
                self.mod.read_local_artifact(str(path))
            self.assertLess(len(str(caught.exception)), 100)
            reader.assert_not_called()
        self.assertTrue(checked)

    def test_foreign_file_uid(self):
        path = self.write("REPORT.md")
        self.assert_foreign_owner_rejected(path, path)
        with tempfile.NamedTemporaryFile(suffix=".json", dir="/tmp") as artifact:
            self.assert_foreign_owner_rejected(Path(artifact.name), Path(artifact.name))

    def test_foreign_directory_uid(self):
        path = self.write("runtime/nested/evidence.json")
        for directory in (self.root, self.root / "runtime", path.parent):
            with self.subTest(directory=directory):
                self.assert_foreign_owner_rejected(path, directory)

    def test_binary_invalid_utf8_and_late_nul(self):
        for data in (
            b"\x00binary",
            b"bad\xff",
            b"\x89PNG\r\n\x1a\n",
            b"%PDF-1.7",
            b"a" * 9000 + b"\x00",
        ):
            path = self.write("binary.txt", data)
            with self.subTest(data=data[:10]), self.assertRaises(ValueError):
                self.mod.read_local_artifact(str(path))

    def test_directory_fifo_socket(self):
        directory = self.root / "directory.md"
        directory.mkdir()
        fifo = self.root / "fifo.txt"
        os.mkfifo(fifo)
        with socket.socket(socket.AF_UNIX) as sock:
            socket_path = self.root / "socket.log"
            sock.bind(str(socket_path))
            for path in (directory, fifo, socket_path):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    self.mod.read_local_artifact(str(path))

    def test_disabled_and_absent(self):
        path = self.write("REPORT.md")
        for section in (
            "",
            "local_artifacts:\n  enabled: false\n",
            self.section.replace("true", "false"),
        ):
            mod = self._import_server(self.config + section)
            with (
                self.subTest(section=section),
                self.assertRaisesRegex(ValueError, "disabled"),
            ):
                mod.read_local_artifact(str(path))

    def test_malformed_configuration(self):
        sections = (
            "null",
            "[]",
            "true",
            "{enabled: true}",
            "{root: /tmp}",
            "{enabled: 1, root: /tmp}",
            "{enabled: true, root: null}",
            "{enabled: true, root: /etc}",
            "{enabled: false, root: /home}",
            "{enabled: true, root: tmp}",
            "{enabled: true, root: /tmp/../tmp}",
            "{enabled: true, root: /tmp, typo: true}",
            "{enabled: false, typo: true}",
            "{enabled: 'true', root: /tmp, allowed_directory_prefixes: [anchor-]}",
            "{enabled: true, root: /home, allowed_directory_prefixes: [anchor-]}",
            "{enabled: true, root: tmp, allowed_directory_prefixes: [anchor-]}",
            "{enabled: true, root: /tmp/../tmp, allowed_directory_prefixes: [anchor-]}",
            "{enabled: true, root: /tmp, allowed_directory_prefixes: []}",
            "{enabled: true, root: /tmp, allowed_directory_prefixes: ['']}",
            "{enabled: true, root: /tmp, allowed_directory_prefixes: ['../']}",
            "{enabled: true, root: /tmp, allowed_directory_prefixes: ['*']}",
            "{enabled: true, root: /tmp, allowed_directory_prefixes: [1]}",
            "{enabled: true, root: /tmp, allowed_directory_prefixes: anchor-}",
            "{enabled: false, root: /etc, allowed_directory_prefixes: [anchor-]}",
            "{enabled: true, root: /tmp, allowed_directory_prefixes: [anchor-], typo: true}",
        )
        for section in sections:
            with (
                self.subTest(section=section),
                self.assertRaisesRegex(RuntimeError, "local_artifacts"),
            ):
                self._import_server(self.config + f"local_artifacts: {section}\n")

    def test_positions_and_bounded_errors(self):
        path = self.write("REPORT.md")
        for line, column in ((0, 0), (1, -1), (True, 0), (1, True), (1, 999), (99, 0)):
            with (
                self.subTest(position=(line, column)),
                self.assertRaises(ValueError) as caught,
            ):
                self.mod.read_local_artifact(str(path), line, column)
            self.assertLess(len(str(caught.exception)), 100)
        with self.assertRaises(ValueError) as caught:
            self.mod.read_local_artifact(str(self.root / "missing.md"))
        self.assertNotIn(str(self.root), str(caught.exception))

    def test_opened_file_cannot_be_redirected_by_symlink_swap(self):
        path = self.write("REPORT.md", b"original")
        original = self.mod._safe_read_file_chunked

        def swap(*args, **kwargs):
            path.unlink()
            path.symlink_to("/etc/passwd")
            return original(*args, **kwargs)

        with patch.object(self.mod, "_safe_read_file_chunked", side_effect=swap):
            self.assertEqual(
                self.mod.read_local_artifact(str(path))["content"], "original"
            )

    def test_directory_symlink_swap_cannot_redirect_open_descriptor(self):
        path = self.write("runtime/report.md", b"original")
        directory = path.parent
        moved = self.root / "moved-runtime"
        real_open = os.open
        swapped = []

        def swap_after_open(name, *args, **kwargs):
            fd = real_open(name, *args, **kwargs)
            if name == "runtime" and not swapped:
                directory.rename(moved)
                directory.symlink_to("/etc", target_is_directory=True)
                swapped.append(fd)
            return fd

        with patch.object(self.mod.os, "open", side_effect=swap_after_open):
            self.assertEqual(
                self.mod.read_local_artifact(str(path))["content"], "original"
            )
        self.assertTrue(swapped)

    def test_mcp_schema_annotations_and_call(self):
        path = self.write("REPORT.md")

        async def check():
            tools = await self.mod.mcp.list_tools()
            self.assertEqual(
                {tool.name for tool in tools}, set(self.mod.EXPECTED_TOOLS)
            )
            self.assertEqual(len(tools), 12)
            tool = next(tool for tool in tools if tool.name == "read_local_artifact")
            self.assertEqual(tool.inputSchema["required"], ["path"])
            self.assertEqual(
                set(tool.inputSchema["properties"]),
                {"path", "start_line", "start_column"},
            )
            self.assertEqual(tool.inputSchema["properties"]["start_line"]["default"], 1)
            self.assertEqual(
                tool.inputSchema["properties"]["start_column"]["default"], 0
            )
            self.assertTrue(tool.annotations.readOnlyHint)
            self.assertTrue(tool.annotations.idempotentHint)
            self.assertFalse(tool.annotations.destructiveHint)
            self.assertFalse(tool.annotations.openWorldHint)
            result = await self.mod.mcp.call_tool(
                "read_local_artifact", {"path": str(path)}
            )
            self.assertIn("report", str(result))

        asyncio.run(check())
