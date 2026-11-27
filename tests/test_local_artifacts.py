from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import io
import json
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp.types import ImageContent, TextContent
from PIL import Image

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

    def write_image(self, name, image_format="PNG", size=(8, 6)):
        path = self.write(name)
        Image.new("RGB", size, "green").save(path, format=image_format)
        return path

    def test_visual_formats_and_nested_paths(self):
        for name, image_format, mime in (
            ("snapshot.png", "PNG", "image/png"),
            ("playwright/deep/photo.JPG", "JPEG", "image/jpeg"),
            ("playwright/deep/capture.webp", "WEBP", "image/webp"),
        ):
            with self.subTest(name=name):
                path = self.write_image(name, image_format)
                metadata, image = self.mod.read_local_visual(str(path))
                self.assertIs(type(metadata), TextContent)
                self.assertIs(type(image), ImageContent)
                self.assertEqual(image.mimeType, mime)
                self.assertEqual(
                    json.loads(metadata.text),
                    {
                        "path": str(path),
                        "detected_format": mime,
                        "mime": mime,
                        "width": 8,
                        "height": 6,
                        "source_size": path.stat().st_size,
                    },
                )
                self.assertEqual(
                    hashlib.sha256(base64.b64decode(image.data)).hexdigest(),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )

    def test_visual_rejects_unsafe_paths(self):
        with self.assertRaisesRegex(ValueError, "Local artifact path is not allowed"):
            self.mod.read_local_visual("/var/tmp/image.png")
        path = self.write_image("nested/image.png")
        file_link = self.root / "link.png"
        file_link.symlink_to(path)
        directory_link = self.root / "linked"
        directory_link.symlink_to(path.parent, target_is_directory=True)
        self.write_image("repo/image.png")
        self.write("worktree/.git", b"gitdir: /some/gitdir\n")
        self.mod.DENY_PATTERNS.append("**/custom-denied.png")
        for name in (
            str(self.root / "nested" / ".." / "image.png"),
            str(file_link),
            str(directory_link / path.name),
            str(self.write_image(".hidden/image.png")),
            str(self.write_image("credentials/image.png")),
            str(self.write_image("custom-denied.png")),
            str(self.repo / "image.png"),
            str(self.write_image("worktree/image.png")),
        ):
            with self.subTest(path=name), self.assertRaises(ValueError):
                self.mod.read_local_visual(name)
        hardlink = self.root / "hardlink.png"
        hardlink.hardlink_to(path)
        with self.assertRaises(ValueError):
            self.mod.read_local_visual(str(hardlink))

    def test_visual_animated_image_returns_first_frame(self):
        path = self.root / "animated.png"
        first = Image.new("RGBA", (8, 6), (12, 34, 56, 78))
        second = Image.new("RGBA", first.size, "blue")
        first.save(path, save_all=True, append_images=[second])
        metadata, image = self.mod.read_local_visual(str(path))
        self.assertEqual(json.loads(metadata.text)["frame"], 1)
        self.assertEqual(image.mimeType, "image/png")
        with Image.open(io.BytesIO(base64.b64decode(image.data))) as frame:
            self.assertEqual(getattr(frame, "n_frames", 1), 1)
            self.assertEqual(frame.getpixel((0, 0)), (12, 34, 56, 78))

    def test_visual_rejects_foreign_owner(self):
        path = self.write_image("nested/image.png")
        real_fstat = os.fstat
        for foreign_path in (path, path.parent):
            target = foreign_path.stat()

            def foreign_stat(fd, target=target):
                info = real_fstat(fd)
                if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                    return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1)
                return info

            with (
                self.subTest(path=foreign_path),
                patch.object(self.mod.os, "fstat", side_effect=foreign_stat),
                self.assertRaises(ValueError),
            ):
                self.mod.read_local_visual(str(path))

    def test_visual_rejects_fake_mismatched_and_damaged_images(self):
        jpeg = self.write_image("valid.jpg", "JPEG").read_bytes()
        png = self.write_image("valid.png").read_bytes()
        webp = self.write_image("valid.webp", "WEBP").read_bytes()
        for name, data in (
            ("fake.png", b"\x89PNG\r\n\x1a\nnot a real PNG"),
            ("wrong.png", jpeg),
            ("broken.png", png[:-15]),
            ("broken.jpg", jpeg[:-10]),
            ("broken.webp", webp[:-10]),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.mod.read_local_visual(str(self.write(name, data)))

    def test_visual_enforces_byte_pixel_and_decompression_limits(self):
        path = self.write_image("image.png", size=(20, 20))
        for key, limit in (
            ("max_visual_file_bytes", path.stat().st_size - 1),
            ("max_visual_image_bytes", path.stat().st_size - 1),
            ("max_render_pixels", 399),
        ):
            with (
                self.subTest(limit=key),
                patch.dict(self.mod.LIMITS, {key: limit}),
                self.assertRaises(ValueError),
            ):
                self.mod.read_local_visual(str(path))
        for pillow_limit in (399, 100):
            with (
                self.subTest(pillow_limit=pillow_limit),
                patch.object(Image, "MAX_IMAGE_PIXELS", pillow_limit),
                self.assertRaises(ValueError),
            ):
                self.mod.read_local_visual(str(path))

    def test_visual_opened_descriptor_survives_path_replacement(self):
        victim = self.write_image("replacement.png", size=(2, 3))
        real_fstat = os.fstat
        for symlink in (False, True):
            path = self.write_image(f"original-{symlink}.png")
            original = path.stat()
            original_digest = hashlib.sha256(path.read_bytes()).hexdigest()

            def swap_after_fstat(fd, path=path, original=original, symlink=symlink):
                info = real_fstat(fd)
                if (info.st_dev, info.st_ino) == (original.st_dev, original.st_ino):
                    path.unlink()
                    if symlink:
                        path.symlink_to(victim)
                    else:
                        path.write_bytes(victim.read_bytes())
                return info

            with (
                self.subTest(symlink=symlink),
                patch.object(self.mod.os, "fstat", side_effect=swap_after_fstat),
            ):
                metadata, image = self.mod.read_local_visual(str(path))
                self.assertEqual(json.loads(metadata.text)["width"], 8)
                self.assertEqual(
                    hashlib.sha256(base64.b64decode(image.data)).hexdigest(),
                    original_digest,
                )
                self.assertEqual(path.is_symlink(), symlink)

    def test_visual_disabled(self):
        path = self.write_image("image.png")
        mod = self._import_server(self.config + "local_artifacts:\n  enabled: false\n")
        with self.assertRaisesRegex(ValueError, "Local artifact access is disabled"):
            mod.read_local_visual(str(path))

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
        for suffix in (".md", ".json", ".txt", ".log", ".csv", ".tsv", ".lean"):
            with self.subTest(suffix=suffix):
                path = self.write("unicode" + suffix, "Привет 🌍\r\n".encode())
                result = self.mod.read_local_artifact(str(path))
                self.assertEqual(result["content"], "Привет 🌍\r\n")
                self.assertEqual(result["encoding"], "utf-8")

    def test_lean_source_directly_in_tmp(self):
        content = "-- Проверка ∀ n\nexample (n : Nat) : n = n := rfl\n"
        with tempfile.NamedTemporaryFile(suffix=".lean", dir="/tmp") as artifact:
            artifact.write(content.encode("utf-8"))
            artifact.flush()
            result = self.mod.read_local_artifact(artifact.name)
        self.assertEqual(result["content"], content)
        self.assertEqual(result["encoding"], "utf-8")
        self.assertIsNone(result["next_line"])
        self.assertIsNone(result["next_column"])

    def test_nested_lean_source_exact_content_and_boms(self):
        content = "import Mathlib\r\n\r\n-- ∀ n, n = n\r\nexample (n : ℕ) : n = n := by\r\n  rfl"
        for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-32"):
            with self.subTest(encoding=encoding):
                path = self.write(
                    "milestone-1/proofs/Main.lean", content.encode(encoding)
                )
                result = self.mod.read_local_artifact(str(path))
                self.assertEqual(result["path"], str(path))
                self.assertEqual(result["content"], content)
                self.assertEqual(result["encoding"], encoding)
                self.assertIsNone(result["next_line"])
                self.assertIsNone(result["next_column"])

    def test_lean_source_pagination_preserves_original(self):
        content = (
            "import Mathlib\r\n-- "
            + "∀ n : ℕ, n = n; " * 80
            + "\r\n"
            + "example (n : ℕ) : n = n := by\r\n  rfl\r\n" * 20
        )
        path = self.write("milestone-2/proofs/Main.lean", content.encode("utf-8"))
        self.mod.LIMITS["max_context_chars"] = 97
        chunks = []
        positions = set()
        line, column = 1, 0
        for _ in range(len(content) + 1):
            self.assertNotIn((line, column), positions)
            positions.add((line, column))
            result = self.mod.read_local_artifact(str(path), line, column)
            self.assertEqual(
                (result["start_line"], result["start_column"]), (line, column)
            )
            self.assertLessEqual(len(result["content"]), 97)
            chunks.append(result["content"])
            if result["next_line"] is None:
                self.assertIsNone(result["next_column"])
                break
            line, column = result["next_line"], result["next_column"]
        else:
            self.fail("Lean source pagination did not finish")
        self.assertGreater(len(chunks), 1)
        self.assertTrue(any(column > 0 for _, column in positions))
        self.assertEqual("".join(chunks), content)

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
            str(self.root / "milestone" / ".." / "Main.lean"),
            "/var/tmp/Main.lean",
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
            ".env.lean",
            "secrets.lean",
            "credentials.lean",
            "private-key.lean",
            "Main.lean.bak",
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
                link = self.root / f"link{index}.lean"
                link.symlink_to(destination)
                with (
                    self.subTest(destination=destination),
                    self.assertRaises(ValueError),
                ):
                    self.mod.read_local_artifact(str(link))
            link_dir = self.root / "linked"
            link_dir.symlink_to(self.root, target_is_directory=True)
            self.write("real.lean")
            with self.assertRaises(ValueError):
                self.mod.read_local_artifact(str(link_dir / "real.lean"))

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
        path = self.write("Main.lean")
        self.assert_foreign_owner_rejected(path, path)
        with tempfile.NamedTemporaryFile(suffix=".json", dir="/tmp") as artifact:
            self.assert_foreign_owner_rejected(Path(artifact.name), Path(artifact.name))

    def test_foreign_directory_uid(self):
        path = self.write("runtime/nested/Main.lean")
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
            for suffix in (".txt", ".lean"):
                path = self.write("binary" + suffix, data)
                with (
                    self.subTest(data=data[:10], suffix=suffix),
                    self.assertRaises(ValueError),
                ):
                    self.mod.read_local_artifact(str(path))

    def test_directory_fifo_socket(self):
        directory = self.root / "directory.lean"
        directory.mkdir()
        fifo = self.root / "fifo.lean"
        os.mkfifo(fifo)
        with socket.socket(socket.AF_UNIX) as sock:
            socket_path = self.root / "socket.lean"
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
            self.assertEqual(len(tools), 15)
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
