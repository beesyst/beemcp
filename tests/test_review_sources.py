from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_read_only_tools as fixtures

_run_git = fixtures._run_git


class ReviewSourcesTests(unittest.TestCase):
    _init_repo = fixtures.ReadOnlyToolsTests._init_repo
    _commit_file = fixtures.ReadOnlyToolsTests._commit_file
    _make_project_yml = fixtures.ReadOnlyToolsTests._make_project_yml
    _import_server = fixtures.ReadOnlyToolsTests._import_server

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self._init_repo("sources")
        self._commit_file(self.repo, "base.txt", "base\n")
        config = self._make_project_yml(
            f"  sources:\n    source: {self.repo}\n    base_branch: main\n"
            "    context_modes:\n      A:\n        files: []\n      B:\n        files: []\n"
        )
        self.mod = self._import_server(config)
        self.mod.LIMITS["max_review_page_chars"] = 32

    def pages(self):
        cursor = ""
        snapshots = set()
        for _ in range(2000):
            page = self.mod.get_review_sources_page("sources", "A", cursor)
            self.assertLessEqual(
                len(page["content"]), self.mod.LIMITS["max_review_page_chars"]
            )
            self.assertFalse(page["truncated"])
            self.assertEqual(page["cursor"], cursor)
            snapshots.add(page["snapshot_id"])
            self.assertEqual(len(snapshots), 1)
            yield page
            if not page["has_more"]:
                self.assertIsNone(page["next_cursor"])
                return
            self.assertTrue(page["next_cursor"])
            self.assertNotEqual(page["next_cursor"], cursor)
            cursor = page["next_cursor"]
        self.fail("Source pagination did not finish")

    def test_all_categories_working_tree_order_deduplication_and_transition(self):
        _run_git(["git", "checkout", "-b", "feature"], self.repo)
        self._commit_file(self.repo, "committed.txt", "committed\n")
        (self.repo / "base.txt").write_text("index version\n")
        _run_git(["git", "add", "base.txt"], self.repo)
        working = "working version\n" * 8
        (self.repo / "base.txt").write_text(working)
        (self.repo / "z-untracked.txt").write_text("untracked\n")
        pages = list(self.pages())
        paths = list(dict.fromkeys(p["path"] for p in pages))
        self.assertEqual(paths, ["base.txt", "committed.txt", "z-untracked.txt"])
        self.assertEqual(
            "".join(p["content"] for p in pages if p["path"] == "base.txt"), working
        )
        self.assertTrue(pages[0]["index_may_differ"])
        self.assertTrue(all(p["source"] == "working_tree" for p in pages))
        committed = next(p for p in pages if p["path"] == "committed.txt")
        self.assertEqual((committed["start_line"], committed["start_column"]), (1, 0))
        manifest = self.mod._git_inventory("sources", "A")
        self.assertEqual(paths, manifest["changed_files"])
        diff = self.mod._build_review_diff(manifest, truncate=False)[0]
        self.assertIn("index version", diff)
        self.assertIn("working version", diff)

    def test_large_lean_exact_multiline_and_long_lines(self):
        self.mod.LIMITS["max_review_page_chars"] = 60000
        self.mod.LIMITS["max_context_chars"] = 17
        text = ("-- λ proof\r\n" * 33001) + "α" * 120003 + "\r\nlast"
        (self.repo / "GraphFoundations.lean").write_bytes(text.encode("utf-8"))
        self.assertGreater((self.repo / "GraphFoundations.lean").stat().st_size, 200000)
        pages = list(self.pages())
        self.assertEqual("".join(p["content"] for p in pages), text)
        self.assertTrue(any(p["start_column"] > 60000 for p in pages))
        for current, following in zip(pages, pages[1:]):
            self.assertEqual(
                (current["next_line"], current["next_column"]),
                (following["start_line"], following["start_column"]),
            )
        self.assertEqual(pages[-1]["end_line"], 33003)

    def test_stale_oversized_untracked_and_tracked(self):
        self.mod.LIMITS["max_review_page_chars"] = 60000
        for tracked in (False, True):
            with self.subTest(tracked=tracked):
                path = self.repo / "big.lean"
                if tracked:
                    self._commit_file(self.repo, "big.lean", "-- base\n")
                path.write_text("x" * 200001)
                first = self.mod.get_review_sources_page("sources", "A")
                path.write_text("x" * 200000 + "y")
                with self.assertRaises(self.mod.CursorError) as caught:
                    self.mod.get_review_sources_page(
                        "sources", "A", first["next_cursor"]
                    )
                self.assertEqual(caught.exception.stage, "stale_snapshot")

    def test_stale_oversized_index_with_unchanged_working_source(self):
        self.mod.LIMITS["max_review_page_chars"] = 60000
        path = self.repo / "big.lean"
        path.write_text("a" * 200001)
        _run_git(["git", "add", "big.lean"], self.repo)
        path.write_text("working" * 30000)
        first = self.mod.get_review_sources_page("sources", "A")
        path.write_text("b" * 200001)
        _run_git(["git", "add", "big.lean"], self.repo)
        path.write_text("working" * 30000)
        with self.assertRaises(self.mod.CursorError) as caught:
            self.mod.get_review_sources_page("sources", "A", first["next_cursor"])
        self.assertEqual(caught.exception.stage, "stale_snapshot")

    def test_skip_deleted_binary_encoding_visual_denied_and_symlinks(self):
        self._commit_file(self.repo, "deleted.txt", "gone")
        (self.repo / "deleted.txt").unlink()
        (self.repo / "binary.dat").write_bytes(b"binary\x00data")
        (self.repo / "unsupported.txt").write_bytes(b"\xffinvalid")
        (self.repo / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (self.repo / ".env").write_text("SECRET_TOKEN")
        (self.repo / "private.key").write_text("SECRET_KEY")
        (self.root / "outside.txt").write_text("OUTSIDE_TOKEN")
        (self.repo / "escape.txt").symlink_to(self.root / "outside.txt")
        (self.repo / "secret-alias.txt").symlink_to(self.repo / ".env")
        (self.repo / "broken.txt").symlink_to(self.root / "missing.txt")
        (self.repo / "ok.txt").write_text("okay")
        pages = list(self.pages())
        skips = {
            item["path"]: item["reason"]
            for page in pages
            for item in page["skipped_files"]
        }
        self.assertEqual(
            skips,
            {
                "deleted.txt": "deleted",
                "binary.dat": "binary",
                "unsupported.txt": "unsupported_encoding",
                "image.png": "visual",
                ".env": "denied",
                "private.key": "denied",
                "escape.txt": "escaping",
                "secret-alias.txt": "denied",
                "broken.txt": "broken",
            },
        )
        content = "".join(p["content"] for p in pages)
        self.assertEqual(content, "okay")
        self.assertNotIn("SECRET", str(pages))
        self.assertNotIn("OUTSIDE_TOKEN", str(pages))

    def test_deleted_index_path_recreated_in_working_tree(self):
        self._commit_file(self.repo, "recreated.txt", "index version")
        _run_git(["git", "rm", "recreated.txt"], self.repo)
        (self.repo / "recreated.txt").write_text("current source")
        page = list(self.pages())[0]
        self.assertEqual(page["content"], "current source")
        self.assertEqual(page["skipped_files"], [])
        self.assertTrue(page["index_may_differ"])

    def test_rename_from_denied_path_stays_redacted(self):
        self._commit_file(self.repo, ".env", "PRIVATE_TOKEN")
        _run_git(["git", "mv", ".env", "safe.txt"], self.repo)
        pages = list(self.pages())
        self.assertTrue(pages)
        self.assertTrue(all(p["skipped_files"] for p in pages))
        self.assertNotIn("PRIVATE_TOKEN", str(pages))

    def test_late_decoding_failure_is_explicit_skip(self):
        self.mod.LIMITS["max_review_page_chars"] = 60000
        (self.repo / "late.txt").write_bytes(b"a" * 150000 + b"\xff")
        pages = list(self.pages())
        self.assertTrue(pages[0]["content"])
        self.assertEqual(
            pages[-1]["skipped_files"],
            [{"path": "late.txt", "reason": "unsupported_encoding"}],
        )
        self.assertFalse(pages[-1]["has_more"])

    def test_empty_inventory_and_empty_source(self):
        page = self.mod.get_review_sources_page("sources", "A")
        self.assertIsNone(page["path"])
        self.assertEqual(page["content"], "")
        self.assertEqual(page["skipped_files"], [])
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_cursor"])
        (self.repo / "empty.txt").write_text("")
        page = list(self.pages())[0]
        self.assertEqual(page["path"], "empty.txt")
        self.assertEqual(page["content"], "")
        self.assertEqual(page["skipped_files"], [])

    def test_cursor_rejections_and_traversal(self):
        (self.repo / "a.txt").write_text("a" * 100 + "\nend")
        first = self.mod.get_review_sources_page("sources", "A")
        payload = self.mod._decode_cursor_payload(first["next_cursor"])
        for changes in (
            {"target": "../outside"},
            {"mode": "B"},
            {"kind": "diff"},
            {"file_index": -1},
            {"file_index": 100},
            {"file_index": "../.env"},
            {"file_index": True},
            {"line": 0},
            {"line": 999},
            {"column": -1},
            {"column": 999},
            {"column": True},
            {"page": -1},
            {"line": 2, "column": 3},
        ):
            with self.subTest(changes=changes):
                data = {**payload, **changes}
                cursor = self.mod._encode_cursor(
                    **{
                        "kind": data.pop("kind"),
                        "target": data.pop("target"),
                        "mode": data.pop("mode"),
                        "snapshot_id": data.pop("snapshot_id"),
                        "page": data.pop("page"),
                        **data,
                    }
                )
                with self.assertRaises(self.mod.CursorError):
                    self.mod.get_review_sources_page("sources", "A", cursor)
        for cursor in ("!!!", "e30", "W10", "bnVsbA"):
            with self.subTest(cursor=cursor), self.assertRaises(self.mod.CursorError):
                self.mod.get_review_sources_page("sources", "A", cursor)
        with self.assertRaises(ValueError):
            self.mod.get_review_sources_page("../outside", "A")
        with self.assertRaises(RuntimeError):
            self.mod.read_project_file("sources", "../outside.txt")

    def test_mutation_during_final_page_fails(self):
        path = self.repo / "a.txt"
        path.write_text("before")
        original = self.mod._safe_read_file_chunked

        def read_and_modify(*args, **kwargs):
            result = original(*args, **kwargs)
            path.write_text("after!")
            return result

        with patch.object(
            self.mod, "_safe_read_file_chunked", side_effect=read_and_modify
        ):
            with self.assertRaises(self.mod.CursorError) as caught:
                self.mod.get_review_sources_page("sources", "A")
        self.assertEqual(caught.exception.stage, "stale_snapshot")

    def test_existing_tools_and_mcp_contract(self):
        (self.repo / "new.txt").write_text("new\n")
        self.mod.LIMITS["max_review_page_chars"] = 60000
        manifest = self.mod.get_review_manifest("sources", "A")
        diff = self.mod.get_review_bundle_page("sources", "A")
        source = self.mod.get_review_sources_page("sources", "A")
        self.assertEqual(manifest["snapshot_id"], diff["snapshot_id"])
        self.assertEqual(manifest["snapshot_id"], source["snapshot_id"])
        self.assertIn("new.txt", json.loads(manifest["content"])["untracked"])
        self.assertIn("new", diff["content"])
        self.assertEqual(
            self.mod.read_project_file("sources", "new.txt")["content"], "new\n"
        )
        self.assertIn("new", self.mod.get_review_bundle("sources", "A")["content"])
        self.assertTrue(self.mod.list_worktrees("sources"))

        async def check():
            tools = await self.mod.mcp.list_tools()
            self.assertEqual({t.name for t in tools}, set(self.mod.EXPECTED_TOOLS))
            tool = next(t for t in tools if t.name == "get_review_sources_page")
            self.assertTrue(tool.annotations.readOnlyHint)
            self.assertFalse(tool.annotations.destructiveHint)
            result = await self.mod.mcp.call_tool(
                "get_review_sources_page", {"target": "sources", "mode": "A"}
            )
            self.assertIn("new", str(result))

        asyncio.run(check())
