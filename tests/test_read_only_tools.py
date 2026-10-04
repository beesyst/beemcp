from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

TMP_ROOT: Path | None = None
ROOT = Path(__file__).resolve().parents[1]

for value in (os.environ.get("TMPDIR"), "/dev/shm", "/tmp"):
    if value is None:
        continue

    candidate = Path(value)
    if not candidate.is_dir():
        continue

    try:
        probe = Path(
            tempfile.mkdtemp(
                prefix="bee-mcp-probe-",
                dir=candidate,
            )
        )
    except OSError:
        continue

    probe.rmdir()
    TMP_ROOT = candidate
    break


def _run_git(argv: list[str], cwd: Path) -> str:
    cp = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=True)
    return cp.stdout.strip()


class ReadOnlyToolsTests(unittest.TestCase):
    _module = None

    @classmethod
    def setUpClass(cls) -> None:
        if TMP_ROOT is None:
            raise unittest.SkipTest("no writable temporary storage")
        cls.root = Path(tempfile.mkdtemp(prefix="bee-mcp-tests-", dir=TMP_ROOT))
        cls.addClassCleanup(cls._cleanup)

    @classmethod
    def _cleanup(cls) -> None:
        import shutil

        shutil.rmtree(cls.root, ignore_errors=True)

    def _init_repo(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True)
        _run_git(["git", "init", "-b", "main"], path)
        _run_git(["git", "config", "user.email", "test@test"], path)
        _run_git(["git", "config", "user.name", "Test"], path)
        return path

    def _commit_file(self, repo: Path, rel: str, content: str = "content") -> None:
        full = repo / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        _run_git(["git", "add", rel], repo)
        _run_git(["git", "commit", "-m", f"add {rel}"], repo)

    def _make_project_yml(self, projects_config: str) -> str:
        return (
            "limits:\n"
            "  max_file_bytes: 200000\n"
            "  max_diff_chars: 120000\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 30000\n"
            "deny_paths:\n"
            '  - ".env"\n'
            '  - ".env.*"\n'
            '  - "**/.env"\n'
            '  - "**/.env.*"\n'
            '  - "*.pem"\n'
            '  - "*.key"\n'
            '  - "id_rsa"\n'
            '  - "id_ed25519"\n'
            '  - ".git"\n'
            '  - ".git/**"\n'
            '  - "**/__pycache__/**"\n'
            '  - ".venv/**"\n'
            '  - "**/.venv/**"\n'
            "projects:\n" + projects_config
        )

    def _import_server(self, config_text: str):
        import importlib.util

        server_py = Path(__file__).resolve().parents[1] / "server.py"
        config_path = self.root / "_test_config.yml"
        config_path.write_text(config_text, encoding="utf-8")
        old_config = os.environ.get("BEEMCP_CONFIG")
        old_host = os.environ.get("BEEMCP_HOST")
        old_port = os.environ.get("BEEMCP_PORT")
        old_token = os.environ.get("BEEMCP_GITHUB_TOKEN")
        os.environ["BEEMCP_CONFIG"] = str(config_path)
        os.environ["BEEMCP_HOST"] = "127.0.0.1"
        os.environ["BEEMCP_PORT"] = "8765"
        os.environ["BEEMCP_GITHUB_TOKEN"] = "github_test_token_placeholder"
        try:
            spec = importlib.util.spec_from_file_location("_test_server", server_py)
            if spec is None or spec.loader is None:
                raise AssertionError("Cannot load server.py")
            loader = spec.loader
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            return module
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

    def test_list_projects_returns_allowlisted_projects_and_modes(self) -> None:
        repo = self._init_repo("proj_a")
        self._commit_file(repo, "README.md")
        config = self._make_project_yml(
            "  proj_a:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files:\n"
            "          - README.md\n"
        )
        mod = self._import_server(config)
        result = mod.list_projects()
        self.assertIn("projects", result)
        names = [p["project"] for p in result["projects"]]
        self.assertIn("proj_a", names)
        proj_a = [p for p in result["projects"] if p["project"] == "proj_a"][0]
        self.assertIn("context_modes", proj_a)
        self.assertIn("A", proj_a["context_modes"])
        self.assertNotIn("secrets", str(result))

    def test_relative_source_allows_unavailable_sibling_until_accessed(self) -> None:
        repo = self._init_repo("relative_source")
        self._commit_file(repo, "README.md", "relative source")
        config = self._make_project_yml(
            "  existing:\n"
            "    source: relative_source\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files:\n"
            "          - README.md\n"
            "  unavailable:\n"
            "    source: missing-sibling\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )

        mod = self._import_server(config)

        context = mod.get_project_context(target="existing", mode="A")
        self.assertEqual(context["files"]["README.md"], "relative source")
        with self.assertRaises(RuntimeError):
            mod.list_worktrees(project="unavailable")

    def test_list_worktrees_detects_main_and_linked(self) -> None:
        main_repo = self._init_repo("wt_main")
        self._commit_file(main_repo, "file.txt")
        wt_path = self.root / "wt_linked"
        _run_git(
            ["git", "worktree", "add", "--detach", str(wt_path), "main"], main_repo
        )
        config = self._make_project_yml(
            "  wt_main:\n"
            "    source: " + str(main_repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        result = mod.list_worktrees(project="wt_main")
        self.assertEqual(result["project"], "wt_main")
        targets = [w["target"] for w in result["worktrees"]]
        self.assertIn("wt_main", targets)
        self.assertIn(wt_path.name, targets)
        main = next(item for item in result["worktrees"] if item["is_main"])
        linked = next(item for item in result["worktrees"] if not item["is_main"])
        self.assertEqual(Path(main["path"]), main_repo.resolve())
        self.assertTrue(Path(main["path"]).is_absolute())
        self.assertEqual(main["target"], "wt_main")
        self.assertEqual(Path(linked["path"]), wt_path.resolve())
        self.assertTrue(Path(linked["path"]).is_absolute())
        self.assertEqual(linked["target"], wt_path.name)
        for item in (main, linked):
            resolved = mod._resolve_target(item["target"])
            context = mod.get_project_context(item["target"], "A")
            for key in ("path", "branch", "head", "is_main", "dirty"):
                self.assertEqual(context[key], resolved[key])
                self.assertEqual(item[key], resolved[key])

    def test_target_linked_worktree_equals_basename(self) -> None:
        main_repo = self._init_repo("wt_base")
        self._commit_file(main_repo, "f.txt")
        branch_name = "feature-x"
        _run_git(["git", "checkout", "-b", branch_name], main_repo)
        self._commit_file(main_repo, "g.txt")
        _run_git(["git", "checkout", "main"], main_repo)
        wt_path = self.root / "wt_base-pr123"
        _run_git(["git", "worktree", "add", str(wt_path), branch_name], main_repo)
        config = self._make_project_yml(
            "  wt_base:\n"
            "    source: " + str(main_repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        result = mod.list_worktrees(project="wt_base")
        linked = [w for w in result["worktrees"] if not w["is_main"]]
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0]["target"], "wt_base-pr123")

    def test_worktree_other_repo_not_accepted(self) -> None:
        repo_a = self._init_repo("wto_a")
        self._commit_file(repo_a, "a.txt")
        repo_b = self._init_repo("wto_b")
        self._commit_file(repo_b, "b.txt")
        config = self._make_project_yml(
            "  wto_a:\n"
            "    source: " + str(repo_a) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        with self.assertRaises(ValueError):
            mod._resolve_target("wto_b")

    def test_unknown_target_raises_value_error(self) -> None:
        repo = self._init_repo("known_only")
        self._commit_file(repo, "x.txt")
        config = self._make_project_yml(
            "  known_only:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        with self.assertRaises(ValueError):
            mod._resolve_target("nonexistent_target")

    def test_mode_A_reads_only_configured_files(self) -> None:
        repo = self._init_repo("mode_a")
        self._commit_file(repo, "README.md", "# Readme")
        self._commit_file(repo, "SECRET.txt", "shh")
        config = self._make_project_yml(
            "  mode_a:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files:\n"
            "          - README.md\n"
        )
        mod = self._import_server(config)
        result = mod.get_project_context(target="mode_a", mode="A")
        self.assertIn("README.md", result["files"])
        self.assertNotIn("SECRET.txt", result["files"])

    def test_mode_B_inherits_A(self) -> None:
        repo = self._init_repo("mode_b")
        self._commit_file(repo, "README.md", "# R")
        self._commit_file(repo, "config.yml", "key: val")
        config = self._make_project_yml(
            "  mode_b:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files:\n"
            "          - README.md\n"
            "      B:\n"
            "        extends: A\n"
            "        files:\n"
            "          - config.yml\n"
        )
        mod = self._import_server(config)
        result = mod.get_project_context(target="mode_b", mode="B")
        self.assertIn("README.md", result["files"])
        self.assertIn("config.yml", result["files"])

    def test_mode_C_returns_bounded_tree_not_full_repo(self) -> None:
        repo = self._init_repo("mode_c")
        self._commit_file(repo, "README.md", "# R")
        self._commit_file(repo, "src/main.py", "print('hello')")
        self._commit_file(repo, "src/util.py", "def f(): pass")
        self._commit_file(repo, "docs/guide.md", "# Guide")
        config = self._make_project_yml(
            "  mode_c:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files:\n"
            "          - README.md\n"
            "      C:\n"
            "        extends: A\n"
            "        tree_roots:\n"
            "          - src\n"
        )
        mod = self._import_server(config)
        result = mod.get_project_context(target="mode_c", mode="C")
        self.assertIn("README.md", result["files"])
        self.assertIn("repository_tree", result)
        tree = result["repository_tree"]
        self.assertIn("src/main.py", tree)
        self.assertIn("src/util.py", tree)
        self.assertNotIn("docs/guide.md", tree)

    def test_directory_entry_recursively_loads_direct_and_nested_text_file(
        self,
    ) -> None:
        repo = self._init_repo("dir_expand")
        self._commit_file(repo, "README.md", "# R")
        self._commit_file(repo, "docs/guide.md", "# Guide")
        self._commit_file(repo, "docs/api/v1/index.md", "# API v1")
        self._commit_file(repo, "src/main.py", "print('hello')")

        config = self._make_project_yml(
            "  dir_expand:\n"
            f"    source: {repo}\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files:\n"
            "          - README.md\n"
            "      B:\n"
            "        extends: A\n"
            "        files:\n"
            "          - docs/guide.md\n"
            "          - docs/\n"
            "          - src/main.py\n"
        )

        mod = self._import_server(config)
        result = mod.get_project_context(
            target="dir_expand",
            mode="B",
        )

        files = result["files"]
        keys = list(files)

        self.assertEqual(files["docs/guide.md"], "# Guide")
        self.assertEqual(files["docs/api/v1/index.md"], "# API v1")
        self.assertEqual(files["src/main.py"], "print('hello')")
        self.assertEqual(files["README.md"], "# R")
        self.assertEqual(keys.count("docs/guide.md"), 1)
        self.assertNotIn("docs/", files)

        recursive_index = keys.index("docs/api/v1/index.md")
        for explicit in ("docs/guide.md", "src/main.py", "README.md"):
            self.assertLess(keys.index(explicit), recursive_index)

    def test_directory_expansion_respects_max_tree_entries_and_reports_tree_limit(
        self,
    ) -> None:
        repo = self._init_repo("dir_limit")
        for name in ("a.md", "b.md", "c.md", "d.md"):
            self._commit_file(repo, f"docs/{name}", name)

        config = self._make_project_yml(
            "  dir_limit:\n"
            f"    source: {repo}\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files:\n"
            "          - docs\n"
        ).replace("  max_tree_entries: 4000\n", "  max_tree_entries: 3\n", 1)
        mod = self._import_server(config)

        result = mod.get_project_context(target="dir_limit", mode="A")

        self.assertEqual(list(result["files"]), ["docs/a.md", "docs/b.md", "docs/c.md"])
        self.assertIn(
            {"target": "dir_limit", "path": "docs", "reason": "tree_limit"},
            result["omitted_files"],
        )

        (repo / "docs/d.md").unlink()
        result = mod.get_project_context(target="dir_limit", mode="A")

        self.assertEqual(list(result["files"]), ["docs/a.md", "docs/b.md", "docs/c.md"])
        self.assertNotIn(
            {"target": "dir_limit", "path": "docs", "reason": "tree_limit"},
            result["omitted_files"],
        )

    def test_related_roadmap_returned(self) -> None:
        proj_a = self._init_repo("rel_a")
        self._commit_file(proj_a, "docs/ROADMAP.md", "# Roadmap A")
        proj_b = self._init_repo("rel_b")
        self._commit_file(proj_b, "docs/ROADMAP.md", "# Roadmap B")
        config = self._make_project_yml(
            "  rel_a:\n"
            "    source: " + str(proj_a) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files:\n"
            "          - docs/ROADMAP.md\n"
            "    related:\n"
            "      - project: rel_b\n"
            "        files:\n"
            "          - docs/ROADMAP.md\n"
            "  rel_b:\n"
            "    source: " + str(proj_b) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        result = mod.get_project_context(target="rel_a", mode="A")
        self.assertIn("rel_b/docs/ROADMAP.md", result["related_files"])

    def test_review_bundle_includes_committed_branch_changes(self) -> None:
        repo = self._init_repo("rb_commit")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        self._commit_file(repo, "feature.txt", "feature")
        config = self._make_project_yml(
            "  rb_commit:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="rb_commit", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        self.assertIn("feature.txt", manifest["changed_files"])

    def test_bundle_includes_staged_changes(self) -> None:
        repo = self._init_repo("rb_staged")
        self._commit_file(repo, "base.txt", "base")
        (repo / "staged.txt").write_text("staged", encoding="utf-8")
        _run_git(["git", "add", "staged.txt"], repo)
        config = self._make_project_yml(
            "  rb_staged:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="rb_staged", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        self.assertIn("staged.txt", manifest["changed_files"])

    def test_bundle_includes_unstaged_changes(self) -> None:
        repo = self._init_repo("rb_unstaged")
        self._commit_file(repo, "base.txt", "base")
        (repo / "base.txt").write_text("modified", encoding="utf-8")
        config = self._make_project_yml(
            "  rb_unstaged:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="rb_unstaged", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        self.assertIn("base.txt", manifest["changed_files"])

    def test_bundle_includes_safe_untracked_text_files(self) -> None:
        repo = self._init_repo("rb_untracked")
        self._commit_file(repo, "base.txt", "base")
        (repo / "new_file.txt").write_text("untracked", encoding="utf-8")
        config = self._make_project_yml(
            "  rb_untracked:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="rb_untracked", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        self.assertIn("new_file.txt", manifest["untracked"])

    def test_bundle_marks_deleted_file(self) -> None:
        repo = self._init_repo("rb_deleted")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, "todelete.txt", "delete me")
        _run_git(["git", "checkout", "-b", "delete-branch"], repo)
        _run_git(["git", "rm", "todelete.txt"], repo)
        _run_git(["git", "commit", "-m", "delete todelete.txt"], repo)
        config = self._make_project_yml(
            "  rb_deleted:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="rb_deleted", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        result = mod.get_review_bundle(target="rb_deleted", mode="A")
        self.assertIn("todelete.txt", manifest["deleted"])
        self.assertIn("todelete.txt", result["content"])

    def test_bundle_marks_rename(self) -> None:
        repo = self._init_repo("rb_rename")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, "old.txt", "rename me")
        _run_git(["git", "checkout", "-b", "rename-branch"], repo)
        _run_git(["git", "mv", "old.txt", "new.txt"], repo)
        _run_git(["git", "commit", "-m", "rename old to new"], repo)
        config = self._make_project_yml(
            "  rb_rename:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="rb_rename", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        self.assertTrue(
            any(
                r["from"] == "old.txt" and r["to"] == "new.txt"
                for r in manifest["renamed"]
            ),
            msg=f"No rename old.txt->new.txt in {manifest['renamed']}",
        )

    def test_bundle_returns_current_changed_text_file_contents(self) -> None:
        repo = self._init_repo("rb_contents")
        self._commit_file(repo, "base.txt", "base")
        (repo / "base.txt").write_text("updated content", encoding="utf-8")
        config = self._make_project_yml(
            "  rb_contents:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="rb_contents", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        result = mod.get_review_bundle(target="rb_contents", mode="A")
        self.assertIn("base.txt", manifest["changed_files"])
        self.assertIn("updated content", result["content"])

    def test_untracked_env_absent_from_diff_and_file_contents(self) -> None:
        repo = self._init_repo("rb_deny")
        self._commit_file(repo, "base.txt", "base")
        (repo / ".env").write_text("SECRET=key", encoding="utf-8")
        (repo / ".env.example").write_text("PUBLIC=value", encoding="utf-8")
        (repo / "nested").mkdir()
        (repo / "nested/.env.example").write_text(
            "NESTED_SECRET=value",
            encoding="utf-8",
        )
        config = self._make_project_yml(
            "  rb_deny:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="rb_deny", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        result = mod.get_review_bundle(target="rb_deny", mode="A")
        self.assertIn(".env", manifest["redacted_paths"])
        self.assertIn("nested/.env.example", manifest["redacted_paths"])
        self.assertIn(".env.example", manifest["untracked"])
        self.assertIn(".env.example", manifest["changed_files"])
        self.assertIn("PUBLIC=value", result["content"])
        self.assertNotIn("SECRET=key", result.get("content", ""))
        self.assertNotIn("NESTED_SECRET=value", result.get("content", ""))

    def test_tracked_nested_env_absent_from_diff_and_file_contents(self) -> None:
        repo = self._init_repo("rb_nest_env")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "env-branch"], repo)
        sub = repo / "sub"
        sub.mkdir()
        (sub / ".env").write_text("nested=secret", encoding="utf-8")
        _run_git(["git", "add", "sub/.env"], repo)
        _run_git(["git", "commit", "-m", "add nested .env"], repo)
        config = self._make_project_yml(
            "  rb_nest_env:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="rb_nest_env", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        result = mod.get_review_bundle(target="rb_nest_env", mode="A")
        self.assertIn("sub/.env", manifest["redacted_paths"])
        self.assertNotIn("sub/.env", result.get("content", ""))

    def test_untracked_alias_to_env_not_resolved(self) -> None:
        repo = self._init_repo("rb_alias")
        self._commit_file(repo, "base.txt", "base")
        (repo / ".env").write_text("SECRET=key", encoding="utf-8")
        (repo / "alias.txt").symlink_to(".env")
        config = self._make_project_yml(
            "  rb_alias:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="rb_alias", mode="A")
        self.assertNotIn("SECRET=key", result.get("content", ""))

    def test_untracked_escaping_symlink_not_read(self) -> None:
        repo = self._init_repo("rb_escape")
        self._commit_file(repo, "base.txt", "base")
        escape_target = self.root / "outside.txt"
        escape_target.write_text("escaped", encoding="utf-8")
        (repo / "escape.txt").symlink_to(os.path.relpath(escape_target, repo))
        config = self._make_project_yml(
            "  rb_escape:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="rb_escape", mode="A")
        self.assertNotIn("escaped", result.get("content", ""))

    def test_broken_symlink_not_read(self) -> None:
        repo = self._init_repo("rb_broken")
        self._commit_file(repo, "base.txt", "base")
        (repo / "broken.txt").symlink_to("nonexistent")
        config = self._make_project_yml(
            "  rb_broken:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="rb_broken", mode="A")
        self.assertNotIn("broken.txt", result.get("content", ""))

    def test_tracked_binary_file_not_in_diff(self) -> None:
        repo = self._init_repo("rb_binary")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "binary-branch"], repo)
        (repo / "data.bin").write_bytes(b"\x00\x01\x02")
        _run_git(["git", "add", "data.bin"], repo)
        _run_git(["git", "commit", "-m", "add binary"], repo)
        config = self._make_project_yml(
            "  rb_binary:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="rb_binary", mode="A")
        self.assertNotIn("data.bin", result.get("content", ""))

    def test_tracked_oversized_file_not_in_diff(self) -> None:
        repo = self._init_repo("rb_oversized")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "oversized-branch"], repo)
        large = "x" * 300000
        (repo / "large.txt").write_text(large, encoding="utf-8")
        _run_git(["git", "add", "large.txt"], repo)
        _run_git(["git", "commit", "-m", "add large"], repo)
        config = self._make_project_yml(
            "  rb_oversized:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="rb_oversized", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        result = mod.get_review_bundle(target="rb_oversized", mode="A")
        self.assertNotIn("large.txt", result.get("content", ""))
        omitted_paths = {o["path"] for o in manifest["omitted_files"]}
        self.assertIn(
            "large.txt",
            omitted_paths,
            msg=f"large.txt not in omitted: {manifest['omitted_files']}",
        )

    def test_read_project_file_rejects_absolute_path(self) -> None:
        repo = self._init_repo("rpf_abs")
        self._commit_file(repo, "safe.txt", "safe")
        config = self._make_project_yml(
            "  rpf_abs:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        with self.assertRaises((ValueError, RuntimeError)):
            mod.read_project_file(target="rpf_abs", path="/etc/passwd")

    def test_read_project_file_rejects_dot_dot(self) -> None:
        repo = self._init_repo("rpf_dotdot")
        self._commit_file(repo, "safe.txt", "safe")
        config = self._make_project_yml(
            "  rpf_dotdot:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        with self.assertRaises((ValueError, RuntimeError)):
            mod.read_project_file(target="rpf_dotdot", path="../outside.txt")

    def test_search_project_fixed_string(self) -> None:
        repo = self._init_repo("search_fixed")
        self._commit_file(repo, "src/main.py", "def hello(): pass")
        self._commit_file(repo, "README.md", "# Hello World")
        config = self._make_project_yml(
            "  search_fixed:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files:\n"
            "          - src/main.py\n"
            "          - README.md\n"
        )
        mod = self._import_server(config)
        sr = mod.search_project(target="search_fixed", query="hello", mode="A")
        self.assertIn("results", sr)
        self.assertIn("truncated", sr)
        self.assertTrue(len(sr["results"]) >= 1)
        found = any("hello" in r["text"].lower() for r in sr["results"])
        self.assertTrue(found)

    def test_search_project_query_validation(self) -> None:
        repo = self._init_repo("search_val")
        self._commit_file(repo, "f.txt", "content")
        config = self._make_project_yml(
            "  search_val:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files:\n"
            "          - f.txt\n"
        )
        mod = self._import_server(config)
        with self.assertRaises(ValueError):
            mod.search_project(target="search_val", query="", mode="A")
        with self.assertRaises(ValueError):
            too_long = "x" * (mod.LIMITS["max_context_chars"] + 1)
            mod.search_project(target="search_val", query=too_long, mode="A")
        max_len = "x" * mod.LIMITS["max_context_chars"]
        sr = mod.search_project(target="search_val", query=max_len, mode="A")
        self.assertIn("results", sr)

    def test_invalid_project_target_mode_raises_error(self) -> None:
        repo = self._init_repo("valid_proj")
        self._commit_file(repo, "f.txt", "x")
        config = self._make_project_yml(
            "  valid_proj:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        with self.assertRaises(ValueError):
            mod.list_worktrees(project="nonexistent")
        with self.assertRaises((ValueError, RuntimeError)):
            mod._resolve_target("nonexistent")
        with self.assertRaises(ValueError):
            mod.get_project_context(target="valid_proj", mode="Z")

    def test_all_tool_annotations_are_toolannotations(self) -> None:
        from mcp.types import ToolAnnotations

        repo = self._init_repo("ro_check")
        self._commit_file(repo, "f.txt", "x")
        config = self._make_project_yml(
            "  ro_check:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        tools = mod.mcp._tool_manager.list_tools()
        for t in tools:
            self.assertIsInstance(
                t.annotations,
                ToolAnnotations,
                f"Tool {t.name} annotations is not ToolAnnotations",
            )
            self.assertTrue(
                t.annotations.readOnlyHint,
                f"Tool {t.name} is not read-only",
            )
            self.assertFalse(
                t.annotations.destructiveHint,
                f"Tool {t.name} has destructive hint",
            )

    def test_no_copilot_codex_run_check_write_tools(self) -> None:
        repo = self._init_repo("no_agent")
        self._commit_file(repo, "f.txt", "x")
        config = self._make_project_yml(
            "  no_agent:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        tools = mod.mcp._tool_manager.list_tools()
        tool_names = [t.name for t in tools]
        forbidden = [
            "prepare_run",
            "start_copilot_initial",
            "start_codex_fix",
            "get_job",
            "get_diff",
            "read_changed_file",
            "run_checks",
            "discard_run",
        ]
        for name in forbidden:
            self.assertNotIn(name, tool_names, f"Forbidden tool found: {name}")

    def test_health_readiness_routes_work(self) -> None:
        import asyncio

        repo = self._init_repo("health_test")
        self._commit_file(repo, "f.txt", "x")
        config = self._make_project_yml(
            "  health_test:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        app = mod.mcp.streamable_http_app()
        routes = {route.path: route for route in app.routes}
        self.assertIn("/mcp", routes)
        for path, expected_body in (("/healthz", b"live"), ("/readyz", b"ready")):
            response = asyncio.run(routes[path].endpoint(None))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.body, expected_body)
            self.assertEqual(response.media_type, "text/plain")

    def test_versionz_returns_identity_and_no_leaks(self) -> None:
        import asyncio
        import json

        repo = self._init_repo("ver_test")
        self._commit_file(repo, "f.txt", "x")
        config = self._make_project_yml(
            "  ver_test:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        app = mod.mcp.streamable_http_app()
        routes = {route.path: route for route in app.routes}
        self.assertIn("/versionz", routes)
        response = asyncio.run(routes["/versionz"].endpoint(None))
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["service"], "beemcp")
        self.assertIsInstance(data["git_head"], str)
        self.assertTrue(len(data["git_head"]) > 0)
        self.assertEqual(data["tool_contract_version"], 3)
        self.assertEqual(data["tool_count"], 9)
        self.assertEqual(
            data["tool_contract_sha256"],
            mod.tool_contract_fingerprint(mod.EXPECTED_TOOLS),
        )
        response2 = asyncio.run(routes["/versionz"].endpoint(None))
        data2 = json.loads(response2.body)
        self.assertEqual(
            data["tool_contract_sha256"],
            data2["tool_contract_sha256"],
        )
        serialized = json.dumps(data)
        self.assertNotIn(str(repo), serialized)
        self.assertNotIn("/home/", serialized)
        self.assertNotIn("SECRET", serialized)
        self.assertNotIn("token", serialized.lower())
        self.assertNotIn("credential", serialized.lower())
        self.assertNotIn("tunnel", serialized.lower())
        self.assertNotIn("env", serialized.lower())

    def test_mcp_tools_list_contains_exactly_nine_expected(self) -> None:
        repo = self._init_repo("tools_count")
        self._commit_file(repo, "f.txt", "x")
        config = self._make_project_yml(
            "  tools_count:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        tools = mod.mcp._tool_manager.list_tools()
        tool_names = {tool.name for tool in tools}
        self.assertEqual(tool_names, set(mod.EXPECTED_TOOLS))

    def test_module_imports_without_tmpdir(self) -> None:
        import importlib.util

        server_py = Path(__file__).resolve().parents[1] / "server.py"
        old_host = os.environ.get("BEEMCP_HOST")
        old_port = os.environ.get("BEEMCP_PORT")
        old_config = os.environ.get("BEEMCP_CONFIG")
        old_token = os.environ.get("BEEMCP_GITHUB_TOKEN")
        os.environ["BEEMCP_HOST"] = "127.0.0.1"
        os.environ["BEEMCP_PORT"] = "8765"
        os.environ["BEEMCP_GITHUB_TOKEN"] = "github_test_token_placeholder"
        try:
            spec = importlib.util.spec_from_file_location("_server_import", server_py)
            if spec is None or spec.loader is None:
                raise AssertionError("Cannot load server.py for import test")
            loader = spec.loader
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            self.assertTrue(hasattr(module, "list_projects"))
            self.assertTrue(hasattr(module, "get_review_bundle"))
        finally:
            for k, v in [
                ("BEEMCP_HOST", old_host),
                ("BEEMCP_PORT", old_port),
                ("BEEMCP_CONFIG", old_config),
                ("BEEMCP_GITHUB_TOKEN", old_token),
            ]:
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_related_symlink_to_denied_file_not_read(self) -> None:
        main_repo = self._init_repo("rel_main")
        self._commit_file(main_repo, "docs/guide.md", "safe guide")
        related_repo = self._init_repo("rel_sec")
        related_repo_safe = related_repo / "docs"
        related_repo_safe.mkdir(parents=True, exist_ok=True)
        (related_repo / ".env").write_text("SECRET=key", encoding="utf-8")
        (related_repo_safe / "config.txt").symlink_to("../.env")
        _run_git(["git", "add", "-A"], related_repo)
        _run_git(["git", "commit", "-m", "init"], related_repo)
        config = self._make_project_yml(
            "  rel_main:\n"
            "    source: " + str(main_repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files:\n"
            "          - docs/guide.md\n"
            "    related:\n"
            "      - project: rel_sec\n"
            "        files:\n"
            "          - docs/config.txt\n"
            "  rel_sec:\n"
            "    source: " + str(related_repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        result = mod.get_project_context(target="rel_main", mode="A")
        self.assertNotIn("rel_sec/docs/config.txt", result["related_files"])
        self.assertNotIn("SECRET", str(result["related_files"]))

    def test_max_context_chars_limits_response(self) -> None:
        repo = self._init_repo("ctx_lim")
        self._commit_file(repo, "small.txt", "small")
        very_long = "x" * 200000
        self._commit_file(repo, "large.txt", very_long)
        config = (
            "limits:\n"
            "  max_file_bytes: 500000\n"
            "  max_diff_chars: 1000\n"
            "  max_context_chars: 5000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 30000\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  ctx_lim:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files:\n"
            f"          - small.txt\n"
            f"          - large.txt\n"
        )
        mod = self._import_server(config)
        result = mod.get_project_context(target="ctx_lim", mode="A")
        self.assertIn("small.txt", result["files"])
        self.assertNotIn("large.txt", result["files"])
        omitted_paths = {o["path"] for o in result["omitted_files"]}
        self.assertIn("large.txt", omitted_paths)

    def test_merge_base_failure_raises_runtime_error(self) -> None:
        repo = self._init_repo("mg_fail")
        self._commit_file(repo, "f.txt", "x")
        _run_git(["git", "checkout", "--orphan", "orphan"], repo)
        _run_git(["git", "commit", "-m", "orphan commit"], repo)
        # HEAD is on orphan, base_branch is main - no shared history
        config = self._make_project_yml(
            "  mg_fail:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        with self.assertRaises(RuntimeError):
            mod.get_review_bundle(target="mg_fail", mode="A")

    def test_deleted_denied_file_not_in_diff(self) -> None:
        repo = self._init_repo("rb_del_deny")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, ".env", "SECRET=key")
        _run_git(["git", "checkout", "-b", "del-env"], repo)
        _run_git(["git", "rm", ".env"], repo)
        _run_git(["git", "commit", "-m", "delete .env"], repo)
        config = self._make_project_yml(
            "  rb_del_deny:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="rb_del_deny", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        result = mod.get_review_bundle(target="rb_del_deny", mode="A")
        self.assertIn(".env", manifest["deleted"])
        self.assertNotIn(".env", result.get("content", ""))
        self.assertIn(".env", manifest["redacted_paths"])

    def test_shared_context_budget_files_and_related(self) -> None:
        main_repo = self._init_repo("shared_ctx")
        self._commit_file(main_repo, "a.txt", "a" * 3000)
        self._commit_file(main_repo, "b.txt", "b" * 3000)
        related_repo = self._init_repo("shared_ctx_rel")
        self._commit_file(related_repo, "r.txt", "r" * 3000)
        config = (
            "limits:\n"
            "  max_file_bytes: 500000\n"
            "  max_diff_chars: 1000\n"
            "  max_context_chars: 5000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 30000\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  shared_ctx:\n"
            f"    source: {main_repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files:\n"
            f"          - a.txt\n"
            f"          - b.txt\n"
            f"    related:\n"
            f"      - project: shared_ctx_rel\n"
            f"        files:\n"
            f"          - r.txt\n"
            f"  shared_ctx_rel:\n"
            f"    source: {related_repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )
        mod = self._import_server(config)
        result = mod.get_project_context(target="shared_ctx", mode="A")
        total = sum(len(v) for v in result["files"].values())
        total += sum(len(v) for v in result["related_files"].values())
        self.assertLessEqual(total, 5000)

    def test_staged_rename_appears_in_renamed_files(self) -> None:
        repo = self._init_repo("stg_ren")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, "old.txt", "rename me")
        (repo / "old.txt").rename(repo / "new.txt")
        _run_git(["git", "add", "-A"], repo)
        config = self._make_project_yml(
            "  stg_ren:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="stg_ren", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        result = mod.get_review_bundle(target="stg_ren", mode="A")
        self.assertTrue(
            any(
                r["from"] == "old.txt" and r["to"] == "new.txt"
                for r in manifest["renamed"]
            ),
            msg=f"No staged rename in {manifest['renamed']}",
        )
        self.assertIn("old.txt", result["content"])
        self.assertIn("new.txt", result["content"])

    def test_staged_rename_both_paths_in_diff(self) -> None:
        repo = self._init_repo("stg_ren2")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, "old.txt", "rename me")
        _run_git(["git", "mv", "old.txt", "new.txt"], repo)
        config = self._make_project_yml(
            "  stg_ren2:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="stg_ren2", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        result = mod.get_review_bundle(target="stg_ren2", mode="A")
        self.assertTrue(
            any(
                r["from"] == "old.txt" and r["to"] == "new.txt"
                for r in manifest["renamed"]
            ),
            msg=f"No staged rename in {manifest['renamed']}",
        )
        self.assertIn("old.txt", result["content"])
        self.assertIn("new.txt", result["content"])

    def test_rename_from_denied_path_not_leaked(self) -> None:
        repo = self._init_repo("den_ren")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, ".env", "SECRET=key")
        _run_git(["git", "checkout", "-b", "den-ren"], repo)
        _run_git(["git", "mv", ".env", "config.env"], repo)
        _run_git(["git", "commit", "-m", "rename .env to config.env"], repo)
        config = self._make_project_yml(
            "  den_ren:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="den_ren", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        result = mod.get_review_bundle(target="den_ren", mode="A")
        self.assertNotIn("SECRET=key", result.get("content", ""))
        self.assertTrue(
            any(".env" in p for p in manifest.get("redacted_paths", [])),
            msg=f".env not in redacted_paths: {manifest.get('redacted_paths')}",
        )
        self.assertTrue(
            any("config.env" in p for p in manifest.get("redacted_paths", [])),
            msg=f"config.env not in redacted_paths: {manifest.get('redacted_paths')}",
        )

    def _chunk_config(
        self,
        repo: Path,
        name: str,
        max_file_bytes: int = 100,
        max_context_chars: int = 200,
        max_review_page_chars: int = 30000,
    ) -> str:
        return (
            "limits:\n"
            f"  max_file_bytes: {max_file_bytes}\n"
            "  max_diff_chars: 120000\n"
            f"  max_context_chars: {max_context_chars}\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            f"  max_review_page_chars: {max_review_page_chars}\n"
            "deny_paths:\n"
            '  - ".env"\n'
            '  - "**/secret/**"\n'
            "projects:\n"
            f"  {name}:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )

    def test_read_project_file_chunked_sequential(self) -> None:
        repo = self._init_repo("rpf_seq")
        N = 30
        lines = [f"LINE {i:03d} {'x' * 52}\n" for i in range(1, N + 1)]
        full_text = "".join(lines)
        self._commit_file(repo, "big.txt", full_text)

        mod = self._import_server(self._chunk_config(repo, "rpf_seq"))

        chunks: list[str] = []
        sl, sc = 1, 0
        while True:
            r = mod.read_project_file(
                target="rpf_seq", path="big.txt", start_line=sl, start_column=sc
            )
            self.assertEqual(r["path"], "big.txt")
            self.assertEqual(r["start_line"], sl)
            self.assertEqual(r["start_column"], sc)
            chunks.append(r["content"])
            if r["next_line"] is None and r["next_column"] is None:
                break
            sl = r["next_line"]
            sc = r["next_column"]

        self.assertEqual("".join(chunks), full_text)

    def test_read_project_file_cursor_before_newline(self) -> None:
        repo = self._init_repo("rpf_newline")
        self._commit_file(repo, "file.txt", "hello\nworld\n")

        mod = self._import_server(
            self._chunk_config(
                repo,
                "rpf_newline",
                max_context_chars=5,
            )
        )

        first = mod.read_project_file(
            target="rpf_newline",
            path="file.txt",
        )
        second = mod.read_project_file(
            target="rpf_newline",
            path="file.txt",
            start_line=first["next_line"],
            start_column=first["next_column"],
        )

        self.assertEqual(first["content"], "hello")
        self.assertEqual(first["next_line"], 1)
        self.assertEqual(first["next_column"], 5)
        self.assertTrue(second["content"].startswith("\n"))

    def test_read_project_file_invalid_start_line(self) -> None:
        repo = self._init_repo("rpf_inv")
        self._commit_file(repo, "f.txt", "hello\nworld\n")
        mod = self._import_server(self._chunk_config(repo, "rpf_inv"))
        with self.assertRaises(ValueError):
            mod.read_project_file(target="rpf_inv", path="f.txt", start_line=0)
        with self.assertRaises(ValueError):
            mod.read_project_file(
                target="rpf_inv",
                path="f.txt",
                start_line=1,
                start_column=-1,
            )
        with self.assertRaises(ValueError):
            mod.read_project_file(
                target="rpf_inv",
                path="f.txt",
                start_line=1,
                start_column=6,
            )

    def test_read_project_file_denied_path(self) -> None:
        repo = self._init_repo("rpf_den")
        self._commit_file(repo, ".env", "SECRET=key")
        mod = self._import_server(self._chunk_config(repo, "rpf_den"))
        with self.assertRaises(ValueError):
            mod.read_project_file(target="rpf_den", path=".env")

    def test_read_project_file_escaping_symlink(self) -> None:
        repo = self._init_repo("rpf_esc")
        self._commit_file(repo, "safe.txt", "safe")
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        (repo / "escape.txt").symlink_to(os.path.relpath(outside, repo))
        _run_git(["git", "add", "escape.txt"], repo)
        _run_git(["git", "commit", "-m", "add escape symlink"], repo)
        mod = self._import_server(self._chunk_config(repo, "rpf_esc"))
        with self.assertRaises(ValueError):
            mod.read_project_file(target="rpf_esc", path="escape.txt")

    def test_read_project_file_binary_file(self) -> None:
        repo = self._init_repo("rpf_bin")
        self._commit_file(repo, "safe.txt", "safe")
        (repo / "data.bin").write_bytes(b"\x00\x01\x02")
        _run_git(["git", "add", "data.bin"], repo)
        _run_git(["git", "commit", "-m", "add binary"], repo)
        mod = self._import_server(self._chunk_config(repo, "rpf_bin"))
        with self.assertRaises(ValueError):
            mod.read_project_file(target="rpf_bin", path="data.bin")

    def test_search_project_oversized_text_file(self) -> None:
        repo = self._init_repo("srch_ovr")
        lines = [f"SEARCH_ME line {i} {'x' * 50}\n" for i in range(1, 40)]
        full_text = "".join(lines)
        self._commit_file(repo, "big.log", full_text)

        config = (
            "limits:\n"
            "  max_file_bytes: 100\n"
            "  max_diff_chars: 120000\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 30000\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  srch_ovr:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files:\n"
            f"          - big.log\n"
        )
        mod = self._import_server(config)
        sr = mod.search_project(target="srch_ovr", query="SEARCH_ME", mode="A")
        self.assertTrue(
            len(sr["results"]) >= 1, msg="should find matches in oversized file"
        )
        found_lines = {r["line"] for r in sr["results"]}
        self.assertIn(1, found_lines, msg="line 1 should be found")
        self.assertIn(39, found_lines, msg="line 39 should be found")

    def test_single_line_longer_than_max_context_chars(self) -> None:
        repo = self._init_repo("rpf_longln")
        text = "A" * 500
        self._commit_file(repo, "long.txt", text)

        config = (
            "limits:\n"
            "  max_file_bytes: 1000000\n"
            "  max_diff_chars: 120000\n"
            "  max_context_chars: 100\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 30000\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  rpf_longln:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )
        mod = self._import_server(config)

        chunks: list[str] = []
        sl, sc = 1, 0
        while True:
            r = mod.read_project_file(
                target="rpf_longln", path="long.txt", start_line=sl, start_column=sc
            )
            self.assertLessEqual(
                len(r["content"]), 100, msg="chunk exceeds max_context_chars"
            )
            chunks.append(r["content"])
            if r["next_line"] is None and r["next_column"] is None:
                break
            self.assertTrue(
                r["next_line"] != sl or r["next_column"] != sc,
                msg="cursor did not advance",
            )
            sl, sc = r["next_line"], r["next_column"]

        self.assertEqual(
            "".join(chunks), text, msg="reassembled content must match original"
        )

    def test_context_limit_reason_in_omitted_files(self) -> None:
        repo = self._init_repo("gpc_ctxlim")
        content_a = "a" * 3000
        content_b = "b" * 3000
        self._commit_file(repo, "a.txt", content_a)
        self._commit_file(repo, "b.txt", content_b)

        config = (
            "limits:\n"
            "  max_file_bytes: 500000\n"
            "  max_diff_chars: 120000\n"
            "  max_context_chars: 3500\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 30000\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  gpc_ctxlim:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files:\n"
            f"          - a.txt\n"
            f"          - b.txt\n"
        )
        mod = self._import_server(config)
        result = mod.get_project_context(target="gpc_ctxlim", mode="A")

        self.assertIn("omitted_files", result)
        omitted = result["omitted_files"]
        reasons = {o["reason"] for o in omitted}
        paths = {o["path"] for o in omitted}
        self.assertIn(
            "context_limit", reasons, msg="context_limit reason must be present"
        )
        self.assertIn("b.txt", paths, msg="b.txt should be omitted (budget exhausted)")

    def test_related_omitted_files_contains_oversized_reason(self) -> None:
        main_repo = self._init_repo("rel_omit_main")
        self._commit_file(main_repo, "main.txt", "main")
        related_repo = self._init_repo("rel_omit_rel")
        large = "x" * 3000
        self._commit_file(related_repo, "big.txt", large)

        config = (
            "limits:\n"
            "  max_file_bytes: 100\n"
            "  max_diff_chars: 120000\n"
            "  max_context_chars: 50000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 30000\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  rel_omit_main:\n"
            f"    source: {main_repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files:\n"
            f"          - main.txt\n"
            f"    related:\n"
            f"      - project: rel_omit_rel\n"
            f"        files:\n"
            f"          - big.txt\n"
            f"  rel_omit_rel:\n"
            f"    source: {related_repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )
        mod = self._import_server(config)
        result = mod.get_project_context(target="rel_omit_main", mode="A")

        self.assertIn("related_omitted_files", result)
        related_omit = result["related_omitted_files"]
        self.assertTrue(
            len(related_omit) >= 1, msg="should have at least one related omitted entry"
        )
        found = any(
            o["target"] == "rel_omit_rel" and o["path"] == "big.txt"
            for o in related_omit
        )
        self.assertTrue(found, msg="rel_omit_rel/big.txt should be in related_omitted")

    def test_search_finds_match_across_segments(self) -> None:
        repo = self._init_repo("srch_segments")
        content = "x" * 198 + "NEEDLE" + "tail\n"
        self._commit_file(repo, "data.txt", content)

        config = (
            "limits:\n"
            "  max_file_bytes: 500000\n"
            "  max_diff_chars: 120000\n"
            "  max_context_chars: 200\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 4\n"
            "  max_review_page_chars: 30000\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  srch_segments:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files:\n"
            f"          - data.txt\n"
        )

        mod = self._import_server(config)
        result = mod.search_project(
            target="srch_segments",
            query="NEEDLE",
            mode="A",
        )

        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["line"], 1)

    def test_search_total_response_bounded(self) -> None:
        repo = self._init_repo("srch_total")
        lines = [f"MATCH line {i:03d} {'x' * 80}\n" for i in range(1, 200)]
        self._commit_file(repo, "big.log", "".join(lines))

        config = (
            "limits:\n"
            "  max_file_bytes: 500000\n"
            "  max_diff_chars: 120000\n"
            "  max_context_chars: 500\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 30000\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  srch_total:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files:\n"
            f"          - big.log\n"
        )
        mod = self._import_server(config)
        sr = mod.search_project(target="srch_total", query="MATCH", mode="A")
        total = sum(len(r["text"]) for r in sr["results"])
        self.assertLessEqual(
            total, 500, msg=f"total search response too large: {total}"
        )

    def test_review_bundle_omitted_includes_context_and_related(self) -> None:
        repo = self._init_repo("rb_omit_ctx")
        large = "x" * 3000
        self._commit_file(repo, "docs/ROADMAP.md", large)
        related_repo = self._init_repo("rb_omit_rel")
        self._commit_file(related_repo, "docs/ROADMAP.md", large)
        self._commit_file(repo, "base.txt", "base")
        (repo / "base.txt").write_text("modified", encoding="utf-8")

        config = (
            "limits:\n"
            "  max_file_bytes: 100\n"
            "  max_diff_chars: 120000\n"
            "  max_context_chars: 50000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 30000\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  rb_omit_ctx:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files:\n"
            f"          - docs/ROADMAP.md\n"
            f"    related:\n"
            f"      - project: rb_omit_rel\n"
            f"        files:\n"
            f"          - docs/ROADMAP.md\n"
            f"  rb_omit_rel:\n"
            f"    source: {related_repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="rb_omit_ctx", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        result = mod.get_review_bundle(target="rb_omit_ctx", mode="A")

        self.assertIn("omitted_files", manifest)
        self.assertNotIn("related_omitted_files", result)
        self.assertNotIn("context", result)
        self.assertNotIn("related_context", result)
        self.assertNotIn("git_status", result)

    def test_search_serialized_size_respected(self) -> None:
        repo = self._init_repo("srch_ser")
        lines = [
            "simple line\n",
            'line with "quotes" and \\backslash\n',
            "line with unicode: \u00e9\u00e0\u00fc\u00f1\n",
            "line with tab\tand \x01control\x02chars\n",
        ]
        self._commit_file(repo, "data.log", "".join(lines))

        config = (
            "limits:\n"
            "  max_file_bytes: 500000\n"
            "  max_diff_chars: 120000\n"
            "  max_context_chars: 200\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 30000\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  srch_ser:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files:\n"
            f"          - data.log\n"
        )
        mod = self._import_server(config)
        import json

        sr = mod.search_project(target="srch_ser", query="line", mode="A")
        self.assertTrue(len(sr["results"]) > 0, msg="search should find matches")
        serialized = json.dumps(sr, ensure_ascii=False)
        self.assertLessEqual(
            len(serialized),
            200,
            msg=f"serialized search response ({len(serialized)} bytes) "
            f"exceeds max_context_chars=200",
        )

    def test_oversized_rename_not_in_diff(self) -> None:
        repo = self._init_repo("rb_rename_oversized")
        large = "x" * 3000
        self._commit_file(repo, "old.txt", large)
        _run_git(["git", "checkout", "-b", "rename-oversized"], repo)
        _run_git(["git", "mv", "old.txt", "new.txt"], repo)
        _run_git(["git", "commit", "-m", "rename oversized file"], repo)

        config = (
            "limits:\n"
            "  max_file_bytes: 100\n"
            "  max_diff_chars: 120000\n"
            "  max_context_chars: 5000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 30000\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  rb_rename_oversized:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )

        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(
            target="rb_rename_oversized",
            mode="A",
            cursor="",
        )
        manifest = json.loads(_r["content"])
        result = mod.get_review_bundle(
            target="rb_rename_oversized",
            mode="A",
        )

        self.assertTrue(
            any(
                rename["from"] == "old.txt" and rename["to"] == "new.txt"
                for rename in manifest["renamed"]
            )
        )
        self.assertNotIn("old.txt", result["content"])
        self.assertNotIn("new.txt", result["content"])
        self.assertTrue(
            any(
                item["path"] == "new.txt" and item["reason"] == "oversized"
                for item in manifest["omitted_files"]
            )
        )

    def test_config_missing_max_review_page_chars_fails_fast(self) -> None:
        repo = self._init_repo("cfg_no_page")
        self._commit_file(repo, "f.txt", "x")
        config = (
            "limits:\n"
            "  max_file_bytes: 200000\n"
            "  max_diff_chars: 120000\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  cfg_no_page:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )
        with self.assertRaises(RuntimeError):
            self._import_server(config)

    def test_config_max_review_page_chars_not_integer_fails(self) -> None:
        repo = self._init_repo("cfg_not_int")
        self._commit_file(repo, "f.txt", "x")
        config = (
            "limits:\n"
            "  max_file_bytes: 200000\n"
            "  max_diff_chars: 120000\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: not_an_int\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  cfg_not_int:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )
        with self.assertRaises(RuntimeError):
            self._import_server(config)

    def test_config_max_review_page_chars_non_positive_fails(self) -> None:
        repo = self._init_repo("cfg_non_pos")
        self._commit_file(repo, "f.txt", "x")
        config = (
            "limits:\n"
            "  max_file_bytes: 200000\n"
            "  max_diff_chars: 120000\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 0\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  cfg_non_pos:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )
        with self.assertRaises(RuntimeError):
            self._import_server(config)

    def test_manifest_single_committed_file(self) -> None:
        repo = self._init_repo("man_one")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        self._commit_file(repo, "new.txt", "new content")
        config = self._make_project_yml(
            "  man_one:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="man_one", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        self.assertEqual(manifest["target"], "man_one")
        self.assertIn("new.txt", manifest["committed"])
        self.assertIn("new.txt", manifest["changed_files"])
        self.assertEqual(manifest["committed"], ["new.txt"])

    def test_manifest_separates_committed_staged_unstaged_untracked(self) -> None:
        repo = self._init_repo("man_sep")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, "committed.txt", "committed")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        (repo / "committed.txt").write_text("modified committed", encoding="utf-8")
        _run_git(["git", "add", "committed.txt"], repo)
        _run_git(["git", "commit", "-m", "modify committed"], repo)
        (repo / "staged.txt").write_text("staged", encoding="utf-8")
        _run_git(["git", "add", "staged.txt"], repo)
        (repo / "base.txt").write_text("unstaged", encoding="utf-8")
        (repo / "untracked.txt").write_text("untracked", encoding="utf-8")
        config = self._make_project_yml(
            "  man_sep:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="man_sep", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        self.assertIn("committed.txt", manifest["committed"])
        self.assertIn("staged.txt", manifest["staged"])
        self.assertIn("base.txt", manifest["unstaged"])
        self.assertIn("untracked.txt", manifest["untracked"])

    def test_manifest_contains_deleted(self) -> None:
        repo = self._init_repo("man_del")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, "todel.txt", "delete me")
        _run_git(["git", "checkout", "-b", "del-branch"], repo)
        _run_git(["git", "rm", "todel.txt"], repo)
        _run_git(["git", "commit", "-m", "delete"], repo)
        config = self._make_project_yml(
            "  man_del:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="man_del", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        self.assertIn("todel.txt", manifest["deleted"])

    def test_manifest_contains_renamed(self) -> None:
        repo = self._init_repo("man_ren")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, "old.txt", "rename me")
        _run_git(["git", "checkout", "-b", "ren-branch"], repo)
        _run_git(["git", "mv", "old.txt", "new.txt"], repo)
        _run_git(["git", "commit", "-m", "rename"], repo)
        config = self._make_project_yml(
            "  man_ren:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="man_ren", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        self.assertTrue(
            any(
                r["from"] == "old.txt" and r["to"] == "new.txt"
                for r in manifest["renamed"]
            )
        )

    def test_manifest_returns_base_branch_and_merge_base(self) -> None:
        repo = self._init_repo("man_mb")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        self._commit_file(repo, "new.txt", "new")
        config = self._make_project_yml(
            "  man_mb:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="man_mb", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        self.assertEqual(manifest["base_branch"], "main")
        self.assertTrue(len(manifest["merge_base"]) > 0)
        self.assertNotEqual(manifest["head"], manifest["merge_base"])

    def test_manifest_changed_files_deterministic_no_duplicates(self) -> None:
        repo = self._init_repo("man_dedup")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, "both.txt", "both")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        (repo / "both.txt").write_text("modified in branch", encoding="utf-8")
        _run_git(["git", "add", "both.txt"], repo)
        _run_git(["git", "commit", "-m", "modify both"], repo)
        (repo / "both.txt").write_text("unstaged change", encoding="utf-8")
        config = self._make_project_yml(
            "  man_dedup:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="man_dedup", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        self.assertEqual(len(manifest["changed_files"]), 1)
        self.assertEqual(manifest["changed_files"], ["both.txt"])

    def test_manifest_no_file_contents(self) -> None:
        repo = self._init_repo("man_no_fc")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        self._commit_file(repo, "data.txt", "sensitive data")
        config = self._make_project_yml(
            "  man_no_fc:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="man_no_fc", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        self.assertNotIn("file_contents", manifest)
        self.assertNotIn("diff", manifest)
        self.assertNotIn("sensitive data", str(manifest))

    def test_denied_file_not_in_safe_diff(self) -> None:
        repo = self._init_repo("pag_deny")
        self._commit_file(repo, "base.txt", "base")
        (repo / ".env").write_text("SECRET=key", encoding="utf-8")
        config = self._make_project_yml(
            "  pag_deny:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="pag_deny", mode="A")
        self.assertNotIn("SECRET=key", page["content"])

    def test_binary_file_not_in_safe_diff(self) -> None:
        repo = self._init_repo("pag_bin")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "bin-branch"], repo)
        (repo / "data.bin").write_bytes(b"\x00\x01\x02")
        _run_git(["git", "add", "data.bin"], repo)
        _run_git(["git", "commit", "-m", "add binary"], repo)
        config = self._make_project_yml(
            "  pag_bin:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="pag_bin", mode="A")
        self.assertNotIn("data.bin", page["content"])

    def test_escaping_symlink_not_read(self) -> None:
        repo = self._init_repo("pag_esc")
        self._commit_file(repo, "base.txt", "base")
        escape_target = self.root / "outside_pag.txt"
        escape_target.write_text("escaped", encoding="utf-8")
        (repo / "escape.txt").symlink_to(os.path.relpath(escape_target, repo))
        config = self._make_project_yml(
            "  pag_esc:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="pag_esc", mode="A")
        self.assertNotIn("escaped", page["content"])

    def test_oversized_file_omitted(self) -> None:
        repo = self._init_repo("pag_ovr")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "ovr-branch"], repo)
        large = "x" * 300000
        (repo / "large.txt").write_text(large, encoding="utf-8")
        _run_git(["git", "add", "large.txt"], repo)
        _run_git(["git", "commit", "-m", "add large"], repo)
        config = self._make_project_yml(
            "  pag_ovr:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="pag_ovr", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        omitted_paths = {o["path"] for o in manifest["omitted_files"]}
        self.assertIn("large.txt", omitted_paths)

    def test_small_diff_one_page(self) -> None:
        repo = self._init_repo("pag_one")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        self._commit_file(repo, "small.txt", "hello")
        config = self._make_project_yml(
            "  pag_one:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="pag_one", mode="A")
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_cursor"])
        self.assertEqual(page["page_number"], 0)
        self.assertIn("small.txt", page["content"])

    def test_large_diff_multiple_pages(self) -> None:
        repo = self._init_repo("pag_large")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "large-branch"], repo)
        big_content = "\n".join(f"LINE {i} " + "x" * 150 for i in range(50))
        (repo / "big.txt").write_text(big_content, encoding="utf-8")
        _run_git(["git", "add", "big.txt"], repo)
        _run_git(["git", "commit", "-m", "add big file"], repo)
        config = (
            "limits:\n"
            "  max_file_bytes: 10000000\n"
            "  max_diff_chars: 1000\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 500\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  pag_large:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )
        mod = self._import_server(config)
        all_content: list[str] = []
        cursor = ""
        pages = 0
        while True:
            page = mod.get_review_bundle_page(
                target="pag_large", mode="A", cursor=cursor
            )
            self.assertLessEqual(len(page["content"]), 500)
            all_content.append(page["content"])
            pages += 1
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
            self.assertIsNotNone(cursor)
        self.assertGreater(pages, 1)
        full_diff = "".join(all_content)
        self.assertGreater(len(full_diff), 1000)
        self.assertIn("big.txt", full_diff)
        self.assertIn("LINE 0", full_diff)
        self.assertIn("LINE 49", full_diff)
        self.assertNotIn("[TRUNCATED", full_diff)
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_cursor"])
        self.assertFalse(page["truncated"])

    def test_review_bundle_page_full_cursor_chain_across_all_segments(self) -> None:
        repo = self._init_repo("pag_all_segments")
        self._commit_file(repo, "base.txt", "base\n")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        committed = "COMMITTED_TOKEN\n" + ("committed data " * 20 + "\n") * 12
        self._commit_file(repo, "committed_big.txt", committed)
        (repo / "staged.txt").write_text(
            "STAGED_TOKEN\n" + "staged data " * 30,
            encoding="utf-8",
        )
        _run_git(["git", "add", "staged.txt"], repo)
        (repo / "base.txt").write_text(
            "UNSTAGED_TOKEN\n" + "unstaged data " * 30,
            encoding="utf-8",
        )
        (repo / "untracked.txt").write_text(
            "UNTRACKED_TOKEN\n" + "untracked data " * 30,
            encoding="utf-8",
        )
        config = (
            "limits:\n"
            "  max_file_bytes: 10000000\n"
            "  max_diff_chars: 1000\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 120\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  pag_all_segments:\n    source: {repo}\n    base_branch: main\n"
            "    context_modes:\n      A:\n        files: []\n"
        )
        mod = self._import_server(config)

        cursor = ""
        full_content: list[str] = []
        resume_segments: list[str] = []
        while True:
            page = mod.get_review_bundle_page(
                target="pag_all_segments", mode="A", cursor=cursor
            )
            self.assertEqual(page["cursor"], cursor)
            full_content.append(page["content"])
            if not page["has_more"]:
                self.assertIsNone(page["next_cursor"])
                break
            cursor = page["next_cursor"]
            self.assertIsNotNone(cursor)
            self.assertNotIn("=", cursor)
            resume_segments.append(
                mod._decode_diff_cursor(
                    cursor, "pag_all_segments", "A", page["snapshot_id"]
                )["segment"]
            )

        combined = "".join(full_content)
        inventory = mod._git_inventory("pag_all_segments", "A")
        expected = "".join(chunk for _, chunk in mod._stream_diff_segments(inventory))
        self.assertEqual(combined, expected)
        for marker in (
            "COMMITTED_TOKEN",
            "STAGED_TOKEN",
            "UNSTAGED_TOKEN",
            "UNTRACKED_TOKEN",
        ):
            self.assertIn(marker, combined)

        transitions: list[str] = []
        for segment in resume_segments:
            if not transitions or transitions[-1] != segment:
                transitions.append(segment)
        self.assertEqual(transitions[:3], ["committed", "staged", "unstaged"])
        self.assertIn("untracked:0", transitions)
        self.assertLess(transitions.index("unstaged"), transitions.index("untracked:0"))

    def test_cursor_decoder_accepts_legacy_padding_and_reports_stages(self) -> None:
        repo = self._init_repo("pag_cursor_stages")
        self._commit_file(repo, "base.txt", "base")
        config = self._make_project_yml(
            "  pag_cursor_stages:\n"
            f"    source: {repo}\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        base_payload = {
            "kind": "diff",
            "target": "pag_cursor_stages",
            "mode": "A",
            "snapshot_id": "snapshot",
            "page": 1,
            "segment": "committed",
            "offset": 0,
        }

        def encode(payload: dict[str, object]) -> str:
            return base64.urlsafe_b64encode(
                json.dumps(payload, sort_keys=True).encode("utf-8")
            ).decode("ascii")

        for padding in ("=", "=="):
            for length in range(4):
                candidate = encode({**base_payload, "legacy": "x" * length})
                if candidate.endswith(padding) and not (
                    padding == "=" and candidate.endswith("==")
                ):
                    with self.subTest(padding=padding):
                        unpadded = candidate.rstrip("=")
                        self.assertEqual(
                            mod._decode_diff_cursor(
                                candidate,
                                "pag_cursor_stages",
                                "A",
                                "snapshot",
                            ),
                            mod._decode_diff_cursor(
                                unpadded,
                                "pag_cursor_stages",
                                "A",
                                "snapshot",
                            ),
                        )
                    break
            else:
                self.fail(f"could not construct cursor ending in {padding!r}")

        def assert_stage(cursor: str, expected_stage: str) -> None:
            with self.assertRaises(mod.CursorError) as raised:
                mod._decode_diff_cursor(cursor, "pag_cursor_stages", "A", "snapshot")
            self.assertEqual(raised.exception.stage, expected_stage)

        assert_stage("not-valid-base64!!!", "invalid_base64")
        assert_stage(base64.urlsafe_b64encode(b"\xff").decode(), "invalid_utf8")
        assert_stage(base64.urlsafe_b64encode(b"[").decode(), "invalid_json")
        assert_stage(base64.urlsafe_b64encode(b"[]").decode(), "invalid_structure")
        assert_stage(encode({**base_payload, "kind": "manifest"}), "wrong_kind")
        assert_stage(encode({**base_payload, "target": "other"}), "wrong_target")
        assert_stage(encode({**base_payload, "mode": "B"}), "wrong_mode")
        assert_stage(encode({**base_payload, "snapshot_id": "old"}), "stale_snapshot")
        assert_stage(encode({**base_payload, "segment": "unknown"}), "invalid_segment")
        assert_stage(encode({**base_payload, "offset": -1}), "invalid_offset")
        assert_stage(encode({**base_payload, "page": -1}), "invalid_page")

    def test_diff_page_stops_stream_after_page_is_full(self) -> None:
        repo = self._init_repo("stream_page")
        self._commit_file(repo, "base.txt", "base")
        mod = self._import_server(
            self._make_project_yml(
                "  stream_page:\n"
                f"    source: {repo}\n"
                "    base_branch: main\n"
                "    context_modes:\n"
                "      A:\n"
                "        files: []\n"
            )
        )
        state = {"yielded": 0, "closed": False}

        def fake_stream(_inventory: dict[str, object]):
            try:
                for _ in range(100):
                    state["yielded"] += 1
                    yield "committed", "abc"
            finally:
                state["closed"] = True

        with patch.object(mod, "_stream_diff_segments", side_effect=fake_stream):
            content, segment, offset, has_more = mod._build_review_diff_page(
                {}, "", 0, 5
            )
        self.assertEqual(content, "abcab")
        self.assertEqual(segment, "committed")
        self.assertEqual(offset, 5)
        self.assertTrue(has_more)
        self.assertLess(state["yielded"], 100)
        self.assertTrue(state["closed"])

    def test_cursor_validation_malformed(self) -> None:
        repo = self._init_repo("pag_cv")
        self._commit_file(repo, "base.txt", "base")
        config = self._make_project_yml(
            "  pag_cv:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        with self.assertRaises(ValueError):
            mod.get_review_bundle_page(
                target="pag_cv", mode="A", cursor="not-valid-base64!!!"
            )

    def test_cursor_wrong_target_rejected(self) -> None:
        repo_a = self._init_repo("pag_wta")
        self._commit_file(repo_a, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "feat"], repo_a)
        big_a = "\n".join(f"LINE {i} " + "x" * 200 for i in range(500))
        (repo_a / "big_a.txt").write_text(big_a, encoding="utf-8")
        _run_git(["git", "add", "big_a.txt"], repo_a)
        _run_git(["git", "commit", "-m", "add big"], repo_a)
        repo_b = self._init_repo("pag_wtb")
        self._commit_file(repo_b, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "feat"], repo_b)
        (repo_b / "big_b.txt").write_text(big_a, encoding="utf-8")
        _run_git(["git", "add", "big_b.txt"], repo_b)
        _run_git(["git", "commit", "-m", "add big"], repo_b)
        config = (
            "limits:\n"
            "  max_file_bytes: 10000000\n"
            "  max_diff_chars: 500000\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 100\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  pag_wta:\n"
            f"    source: {repo_a}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
            f"  pag_wtb:\n"
            f"    source: {repo_b}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="pag_wta", mode="A", cursor="")
        self.assertTrue(page["has_more"])
        bad_cursor = page["next_cursor"]
        self.assertIsNotNone(bad_cursor)
        with self.assertRaises(ValueError):
            mod.get_review_bundle_page(target="pag_wtb", mode="A", cursor=bad_cursor)

    def test_cursor_stale_snapshot_rejected(self) -> None:
        repo = self._init_repo("pag_stale")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        big = "\n".join(f"LINE {i} xxxxx" for i in range(100))
        (repo / "big.txt").write_text(big, encoding="utf-8")
        _run_git(["git", "add", "big.txt"], repo)
        _run_git(["git", "commit", "-m", "add big"], repo)
        config = (
            "limits:\n"
            "  max_file_bytes: 10000000\n"
            "  max_diff_chars: 500000\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 100\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  pag_stale:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="pag_stale", mode="A", cursor="")
        old_cursor = page["next_cursor"]
        self.assertIsNotNone(old_cursor)
        (repo / "big.txt").write_text(big + "\nEXTRA", encoding="utf-8")
        with self.assertRaises(ValueError):
            mod.get_review_bundle_page(target="pag_stale", mode="A", cursor=old_cursor)

    def test_committed_diff_present(self) -> None:
        repo = self._init_repo("diff_commit")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        self._commit_file(repo, "new.txt", "committed change")
        config = self._make_project_yml(
            "  diff_commit:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="diff_commit", mode="A")
        self.assertIn("new.txt", page["content"])

    def test_staged_diff_present(self) -> None:
        repo = self._init_repo("diff_stag")
        self._commit_file(repo, "base.txt", "base")
        (repo / "staged.txt").write_text("staged change", encoding="utf-8")
        _run_git(["git", "add", "staged.txt"], repo)
        config = self._make_project_yml(
            "  diff_stag:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="diff_stag", mode="A")
        self.assertIn("staged.txt", page["content"])

    def test_unstaged_diff_present(self) -> None:
        repo = self._init_repo("diff_uns")
        self._commit_file(repo, "base.txt", "base")
        (repo / "base.txt").write_text("unstaged change", encoding="utf-8")
        config = self._make_project_yml(
            "  diff_uns:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="diff_uns", mode="A")
        self.assertIn("base.txt", page["content"])

    def test_untracked_safe_file_diff_present(self) -> None:
        repo = self._init_repo("diff_untr")
        self._commit_file(repo, "base.txt", "base")
        (repo / "untracked.txt").write_text("untracked content", encoding="utf-8")
        config = self._make_project_yml(
            "  diff_untr:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="diff_untr", mode="A")
        self.assertIn("untracked.txt", page["content"])

    def test_deleted_safe_file_diff_present(self) -> None:
        repo = self._init_repo("diff_del")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, "todel.txt", "delete me")
        _run_git(["git", "checkout", "-b", "del-branch"], repo)
        _run_git(["git", "rm", "todel.txt"], repo)
        _run_git(["git", "commit", "-m", "delete"], repo)
        config = self._make_project_yml(
            "  diff_del:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="diff_del", mode="A")
        self.assertIn("todel.txt", page["content"])

    def test_renamed_safe_file_diff_present(self) -> None:
        repo = self._init_repo("diff_ren")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, "old.txt", "rename me")
        _run_git(["git", "checkout", "-b", "ren-branch"], repo)
        _run_git(["git", "mv", "old.txt", "new.txt"], repo)
        _run_git(["git", "commit", "-m", "rename"], repo)
        config = self._make_project_yml(
            "  diff_ren:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="diff_ren", mode="A")
        self.assertIn("old.txt", page["content"])
        self.assertIn("new.txt", page["content"])

    def test_max_diff_chars_truncation_explicit(self) -> None:
        repo = self._init_repo("trunc_diff")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "big-branch"], repo)
        big = "\n".join(f"LINE {i} " + "x" * 200 for i in range(2000))
        (repo / "big.txt").write_text(big, encoding="utf-8")
        _run_git(["git", "add", "big.txt"], repo)
        _run_git(["git", "commit", "-m", "add big"], repo)
        config = (
            "limits:\n"
            "  max_file_bytes: 10000000\n"
            "  max_diff_chars: 1000\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 1500\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  trunc_diff:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="trunc_diff", mode="A")
        self.assertTrue(result["truncated"])
        self.assertIn("[TRUNCATED at 1000 characters]", result["content"])

    def test_get_review_bundle_returns_bounded_first_page_and_metadata(self) -> None:
        repo = self._init_repo("bundle_bounded")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        big = "\n".join(f"LINE {i} " + "x" * 100 for i in range(500))
        (repo / "big.txt").write_text(big, encoding="utf-8")
        _run_git(["git", "add", "big.txt"], repo)
        _run_git(["git", "commit", "-m", "add big"], repo)
        config = (
            "limits:\n"
            "  max_file_bytes: 10000000\n"
            "  max_diff_chars: 1000\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 100\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  bundle_bounded:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="bundle_bounded", mode="A")
        self.assertEqual(result["protocol"], "bounded")
        self.assertIn("content", result)
        self.assertIn("cursor", result)
        self.assertIn("next_cursor", result)
        self.assertIn("has_more", result)
        self.assertIn("page_number", result)
        self.assertIn("total_pages", result)
        self.assertIn("truncated", result)
        self.assertIn("snapshot_id", result)
        self.assertNotIn("changed_files", result)
        self.assertEqual(result["page_number"], 0)
        self.assertTrue(result["has_more"])
        self.assertTrue(result["truncated"])
        self.assertIsNone(result["next_cursor"])
        self.assertLessEqual(len(result["content"]), 1000)

    def test_tool_list_contains_new_review_tools(self) -> None:
        repo = self._init_repo("tl_check")
        self._commit_file(repo, "f.txt", "x")
        config = self._make_project_yml(
            "  tl_check:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        tools = mod.mcp._tool_manager.list_tools()
        names = [t.name for t in tools]
        self.assertIn("get_review_manifest", names)
        self.assertIn("get_review_bundle_page", names)
        self.assertIn("get_review_bundle", names)
        self.assertIn("read_project_file", names)
        self.assertIn("list_projects", names)
        self.assertIn("list_worktrees", names)
        self.assertIn("get_project_context", names)
        self.assertIn("search_project", names)

    def test_unstaged_deleted_safe_file_in_diff(self) -> None:
        repo = self._init_repo("udel_safe")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, "todel.txt", "delete me unstaged")
        (repo / "todel.txt").unlink()
        config = self._make_project_yml(
            "  udel_safe:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="udel_safe", mode="A", cursor="")
        self.assertIn("todel.txt", page["content"])

    def test_staged_deleted_safe_file_in_diff(self) -> None:
        repo = self._init_repo("sdel_safe")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, "todel.txt", "delete me staged")
        _run_git(["git", "rm", "todel.txt"], repo)
        config = self._make_project_yml(
            "  sdel_safe:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="sdel_safe", mode="A", cursor="")
        self.assertIn("todel.txt", page["content"])

    def test_deleted_binary_no_crash(self) -> None:
        repo = self._init_repo("dbin_nocrash")
        self._commit_file(repo, "base.txt", "base")
        (repo / "data.bin").write_bytes(b"\x00\xff\xfe\xff\x00\x01\x02")
        _run_git(["git", "add", "data.bin"], repo)
        _run_git(["git", "commit", "-m", "add binary"], repo)
        _run_git(["git", "checkout", "-b", "feature"], repo)
        _run_git(["git", "rm", "data.bin"], repo)
        _run_git(["git", "commit", "-m", "delete binary"], repo)
        config = self._make_project_yml(
            "  dbin_nocrash:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="dbin_nocrash", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        self.assertIn("data.bin", manifest["deleted"])
        omit_reasons = {(o["path"], o["reason"]) for o in manifest["omitted_files"]}
        self.assertIn(("data.bin", "binary"), omit_reasons)
        page = mod.get_review_bundle_page(target="dbin_nocrash", mode="A", cursor="")
        self.assertNotIn("data.bin", page.get("content", ""))

    def test_deleted_binary_oversized_in_omitted(self) -> None:
        repo = self._init_repo("dbin_omit")
        self._commit_file(repo, "base.txt", "base")
        large = "x" * 300000
        self._commit_file(repo, "large.txt", large)
        _run_git(["git", "checkout", "-b", "feature"], repo)
        _run_git(["git", "rm", "large.txt"], repo)
        _run_git(["git", "commit", "-m", "delete large"], repo)
        config = self._make_project_yml(
            "  dbin_omit:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="dbin_omit", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        omitted_paths = {o["path"] for o in manifest["omitted_files"]}
        self.assertIn("large.txt", omitted_paths)

    def test_deleted_blob_byte_safety(self) -> None:
        repo = self._init_repo("dblob_bytes")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, "safe.txt", "safe text to delete")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        _run_git(["git", "rm", "safe.txt"], repo)
        _run_git(["git", "commit", "-m", "delete safe"], repo)
        config = self._make_project_yml(
            "  dblob_bytes:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="dblob_bytes", mode="A", cursor="")
        self.assertIn("safe.txt", page["content"])

    def test_truncation_change_invalidates_cursor(self) -> None:
        repo = self._init_repo("trunc_inv")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        big = "\n".join(f"LINE {i} " + "x" * 200 for i in range(2000))
        (repo / "big.txt").write_text(big, encoding="utf-8")
        _run_git(["git", "add", "big.txt"], repo)
        _run_git(["git", "commit", "-m", "add big"], repo)
        config = (
            "limits:\n"
            "  max_file_bytes: 10000000\n"
            "  max_diff_chars: 1000\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 100\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  trunc_inv:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="trunc_inv", mode="A", cursor="")
        old_cursor = page["next_cursor"]
        self.assertIsNotNone(old_cursor)
        lines = big.splitlines()
        lines[-1] = "LINE 1999 " + "y" * 200
        (repo / "big.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            mod.get_review_bundle_page(target="trunc_inv", mode="A", cursor=old_cursor)

    def test_cursor_wrong_mode_rejected(self) -> None:
        repo = self._init_repo("pag_wmode")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        big = "\n".join(f"LINE {i} " + "x" * 200 for i in range(500))
        (repo / "big.txt").write_text(big, encoding="utf-8")
        _run_git(["git", "add", "big.txt"], repo)
        _run_git(["git", "commit", "-m", "add big"], repo)
        config = (
            "limits:\n"
            "  max_file_bytes: 10000000\n"
            "  max_diff_chars: 500000\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 100\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  pag_wmode:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
            f"      B:\n"
            f"        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="pag_wmode", mode="A", cursor="")
        bad_cursor = page["next_cursor"]
        self.assertIsNotNone(bad_cursor)
        with self.assertRaises(ValueError):
            mod.get_review_bundle_page(target="pag_wmode", mode="B", cursor=bad_cursor)

    def test_get_review_bundle_no_full_manifest(self) -> None:
        repo = self._init_repo("bdl_no_man")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        (repo / "new.txt").write_text("new", encoding="utf-8")
        _run_git(["git", "add", "new.txt"], repo)
        _run_git(["git", "commit", "-m", "add new"], repo)
        config = self._make_project_yml(
            "  bdl_no_man:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="bdl_no_man", mode="A")
        self.assertNotIn("changed_files", result)
        self.assertNotIn("committed", result)
        self.assertNotIn("staged", result)
        self.assertNotIn("unstaged", result)
        self.assertNotIn("untracked", result)
        self.assertNotIn("deleted", result)
        self.assertNotIn("renamed", result)
        self.assertNotIn("redacted_paths", result)
        self.assertNotIn("omitted_files", result)
        self.assertIn("content", result)
        self.assertIn("next_cursor", result)

    def test_truncated_result_within_max_diff_chars(self) -> None:
        repo = self._init_repo("trunc_budget")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        big = "\n".join(f"LINE {i} " + "x" * 200 for i in range(2000))
        (repo / "big.txt").write_text(big, encoding="utf-8")
        _run_git(["git", "add", "big.txt"], repo)
        _run_git(["git", "commit", "-m", "add big"], repo)
        config = (
            "limits:\n"
            "  max_file_bytes: 10000000\n"
            "  max_diff_chars: 1000\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 1500\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  trunc_budget:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="trunc_budget", mode="A")
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["content"]), 1000)
        self.assertIn("[TRUNCATED at 1000 characters]", result["content"])

    def test_staged_delete_uses_head_blob_for_safety(self) -> None:
        repo = self._init_repo("sdel_head_blob")
        self._commit_file(repo, "base.txt", "base")
        small = "safe text"
        self._commit_file(repo, "safe.txt", small)
        _run_git(["git", "checkout", "-b", "feature"], repo)
        large = "x" * 300000
        (repo / "safe.txt").write_text(large, encoding="utf-8")
        _run_git(["git", "add", "safe.txt"], repo)
        _run_git(["git", "commit", "-m", "make oversized"], repo)
        _run_git(["git", "rm", "safe.txt"], repo)
        config = self._make_project_yml(
            "  sdel_head_blob:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="sdel_head_blob", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        self.assertIn("safe.txt", manifest["deleted"])
        omitted_paths = {o["path"] for o in manifest["omitted_files"]}
        self.assertIn("safe.txt", omitted_paths)

    def test_unstaged_delete_uses_index_blob_for_safety(self) -> None:
        repo = self._init_repo("udel_idx_blob")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, "file.txt", "small")
        large = "x" * 300000
        (repo / "file.txt").write_text(large, encoding="utf-8")
        _run_git(["git", "add", "file.txt"], repo)
        (repo / "file.txt").unlink()
        config = self._make_project_yml(
            "  udel_idx_blob:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="udel_idx_blob", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        omitted_paths = {o["path"] for o in manifest["omitted_files"]}
        self.assertIn("file.txt", omitted_paths)
        page = mod.get_review_bundle_page(target="udel_idx_blob", mode="A", cursor="")
        self.assertNotIn("file.txt", page["content"])

    def test_small_max_diff_chars_remains_bounded(self) -> None:
        repo = self._init_repo("small_mdc")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        big = "\n".join(f"LINE {i} " + "x" * 200 for i in range(2000))
        (repo / "big.txt").write_text(big, encoding="utf-8")
        _run_git(["git", "add", "big.txt"], repo)
        _run_git(["git", "commit", "-m", "add big"], repo)
        config = (
            "limits:\n"
            "  max_file_bytes: 10000000\n"
            "  max_diff_chars: 10\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 100\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  small_mdc:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="small_mdc", mode="A")
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["content"]), 10)

    def test_manifest_multiple_pages_reassemble(self) -> None:
        repo = self._init_repo("man_multi")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        for i in range(30):
            (repo / f"file{i:02d}.txt").write_text(
                f"content {i} " + "x" * 3000, encoding="utf-8"
            )
            _run_git(["git", "add", f"file{i:02d}.txt"], repo)
        _run_git(["git", "commit", "-m", "add many files"], repo)
        config = (
            "limits:\n"
            "  max_file_bytes: 10000000\n"
            "  max_diff_chars: 500000\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 200\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  man_multi:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )
        mod = self._import_server(config)
        import json

        full = ""
        cursor = ""
        pages = 0
        while True:
            _r = mod.get_review_manifest(target="man_multi", mode="A", cursor=cursor)
            full += _r["content"]
            pages += 1
            if not _r["has_more"]:
                self.assertIsNone(_r["next_cursor"])
                break
            cursor = _r["next_cursor"]
        self.assertGreater(pages, 1)
        manifest = json.loads(full)
        self.assertIn("changed_files", manifest)
        self.assertEqual(len(manifest["changed_files"]), 30)
        self.assertEqual(len(set(manifest["changed_files"])), 30)

    def test_large_committed_diff_bounded(self) -> None:
        repo = self._init_repo("lg_comm")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        big = "\n".join(f"LINE {i} " + "x" * 200 for i in range(5000))
        (repo / "big.txt").write_text(big, encoding="utf-8")
        _run_git(["git", "add", "big.txt"], repo)
        _run_git(["git", "commit", "-m", "add big"], repo)
        config = (
            "limits:\n"
            "  max_file_bytes: 10000000\n"
            "  max_diff_chars: 500\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 500\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  lg_comm:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="lg_comm", mode="A")
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["content"]), 500)
        self.assertIn("[TRUNCATED", result["content"])

    def test_large_untracked_file_bounded(self) -> None:
        repo = self._init_repo("lg_untr")
        self._commit_file(repo, "base.txt", "base")
        large = "x" * 300000
        (repo / "huge.txt").write_text(large, encoding="utf-8")
        config = (
            "limits:\n"
            "  max_file_bytes: 500000\n"
            "  max_diff_chars: 200\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 200\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  lg_untr:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="lg_untr", mode="A")
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["content"]), 200)
        self.assertIn("huge.txt", result["content"])

    def test_git_failure_not_silent(self) -> None:
        repo = self._init_repo("git_fail")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "--orphan", "orphan"], repo)
        _run_git(["git", "commit", "-m", "orphan"], repo)
        config = self._make_project_yml(
            "  git_fail:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        with self.assertRaises(RuntimeError):
            mod.get_review_bundle_page(target="git_fail", mode="A", cursor="")

    def test_build_diff_stops_after_budget(self) -> None:
        repo = self._init_repo("bdgt_stop")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        (repo / "f.txt").write_text("hello world", encoding="utf-8")
        _run_git(["git", "add", "f.txt"], repo)
        _run_git(["git", "commit", "-m", "add f"], repo)
        config = (
            "limits:\n"
            "  max_file_bytes: 10000000\n"
            "  max_diff_chars: 5\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 100\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  bdgt_stop:\n"
            f"    source: {repo}\n"
            f"    base_branch: main\n"
            f"    context_modes:\n"
            f"      A:\n"
            f"        files: []\n"
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="bdgt_stop", mode="A")
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["content"]), 5)

    def test_stream_git_failure_propagates(self) -> None:
        import unittest.mock

        repo = self._init_repo("strm_fail")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        (repo / "f.txt").write_text("hello", encoding="utf-8")
        _run_git(["git", "add", "f.txt"], repo)
        _run_git(["git", "commit", "-m", "add f"], repo)
        config = self._make_project_yml(
            "  strm_fail:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        inv = mod._git_inventory("strm_fail", "A")
        with unittest.mock.patch.object(
            mod,
            "_stream_git_stdout",
            side_effect=RuntimeError("simulated stream failure"),
        ):
            with self.assertRaises(RuntimeError):
                mod._build_review_diff(inv, truncate=True)
            with self.assertRaises(RuntimeError):
                mod._build_review_snapshot_id(inv)

    def test_oversized_deleted_blob_rejected_before_read(self) -> None:
        repo = self._init_repo("odb_rej")
        large = "x" * 300000
        self._commit_file(repo, "large.txt", large)
        _run_git(["git", "checkout", "-b", "feature"], repo)
        _run_git(["git", "rm", "large.txt"], repo)
        _run_git(["git", "commit", "-m", "delete large"], repo)
        config = self._make_project_yml(
            "  odb_rej:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="odb_rej", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        self.assertIn("large.txt", manifest["deleted"])
        omit_reasons = {(o["path"], o["reason"]) for o in manifest["omitted_files"]}
        self.assertIn(("large.txt", "oversized"), omit_reasons)

    def test_unsafe_historical_blob_excluded_from_diff(self) -> None:
        repo = self._init_repo("unsafe_hist")
        large = "x" * 300000
        self._commit_file(repo, "large.txt", large)
        _run_git(["git", "checkout", "-b", "feature"], repo)
        (repo / "large.txt").write_text("small text", encoding="utf-8")
        _run_git(["git", "add", "large.txt"], repo)
        _run_git(["git", "commit", "-m", "make small"], repo)
        (repo / "large.txt").write_text("staged change", encoding="utf-8")
        _run_git(["git", "add", "large.txt"], repo)
        (repo / "large.txt").write_text("unstaged change", encoding="utf-8")
        config = self._make_project_yml(
            "  unsafe_hist:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="unsafe_hist", mode="A", cursor="")
        self.assertNotIn("large.txt", page["content"])

    def test_rename_from_oversized_excluded(self) -> None:
        repo = self._init_repo("ren_ovr_excl")
        large = "x" * 300000
        self._commit_file(repo, "big.txt", large)
        _run_git(["git", "mv", "big.txt", "small.txt"], repo)
        config = self._make_project_yml(
            "  ren_ovr_excl:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="ren_ovr_excl", mode="A", cursor="")
        self.assertNotIn("big.txt", page["content"])
        self.assertNotIn("small.txt", page["content"])

    def test_literal_pathspec_security(self) -> None:
        repo = self._init_repo("lit_path")
        (repo / ":(glob)*").write_text("safe content", encoding="utf-8")
        _run_git(["git", "--literal-pathspecs", "add", "--", ":(glob)*"], repo)
        _run_git(["git", "commit", "-m", "add literal"], repo)
        self._commit_file(repo, ".env", "SECRET=supersecret")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        (repo / ":(glob)*").write_text("modified safe content", encoding="utf-8")
        _run_git(["git", "--literal-pathspecs", "add", "--", ":(glob)*"], repo)
        _run_git(["git", "commit", "-m", "modify literal"], repo)
        (repo / ".env").write_text("SECRET=leaked", encoding="utf-8")
        config = self._make_project_yml(
            "  lit_path:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(target="lit_path", mode="A", cursor="")
        self.assertIn(":(glob)*", page["content"])
        self.assertIn("modified safe content", page["content"])
        self.assertNotIn("SECRET=leaked", page["content"])
        self.assertNotIn(".env", page["content"])

    def test_rename_config_independence(self) -> None:
        repo = self._init_repo("ren_conf")
        _run_git(["git", "config", "diff.renames", "false"], repo)
        self._commit_file(repo, "old.txt", "rename me")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        _run_git(["git", "mv", "old.txt", "new.txt"], repo)
        _run_git(["git", "commit", "-m", "rename"], repo)
        config = self._make_project_yml(
            "  ren_conf:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        _r = mod.get_review_manifest(target="ren_conf", mode="A", cursor="")
        manifest = json.loads(_r["content"])
        self.assertTrue(
            any(
                r["from"] == "old.txt" and r["to"] == "new.txt"
                for r in manifest["renamed"]
            ),
            msg=f"rename not found with diff.renames=false: {manifest['renamed']}",
        )
        self.assertNotIn("old.txt", manifest.get("deleted", []))

    def test_committed_delete_staged_readd_is_in_safe_diff(self) -> None:
        repo = self._init_repo("layered_branch_staged")
        self._commit_file(repo, "layered.txt", "committed content")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        _run_git(["git", "rm", "layered.txt"], repo)
        _run_git(["git", "commit", "-m", "delete layered file"], repo)
        (repo / "layered.txt").write_text("staged replacement", encoding="utf-8")
        _run_git(["git", "add", "layered.txt"], repo)
        config = self._make_project_yml(
            "  layered_branch_staged:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        page = mod.get_review_bundle_page(
            target="layered_branch_staged", mode="A", cursor=""
        )
        self.assertIn("layered.txt", page["content"])
        self.assertIn("staged replacement", page["content"])

    def test_staged_delete_untracked_readd_preserves_addition(self) -> None:
        repo = self._init_repo("layered_staged_untracked")
        self._commit_file(repo, "layered.txt", "committed content")
        _run_git(["git", "rm", "layered.txt"], repo)
        (repo / "layered.txt").write_text("untracked replacement", encoding="utf-8")
        config = self._make_project_yml(
            "  layered_staged_untracked:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        manifest_page = mod.get_review_manifest(
            target="layered_staged_untracked", mode="A", cursor=""
        )
        manifest = json.loads(manifest_page["content"])
        page = mod.get_review_bundle_page(
            target="layered_staged_untracked", mode="A", cursor=""
        )
        self.assertIn("layered.txt", manifest["deleted"])
        self.assertIn("deleted file mode", page["content"])
        self.assertIn("new file mode", page["content"])
        self.assertIn("untracked replacement", page["content"])

    def test_multilayer_unsafe_deleted_blob_omits_path(self) -> None:
        repo = self._init_repo("layered_unsafe_deleted")
        old_content = "old oversized content\n" + "x" * 200001
        self._commit_file(repo, "layered.txt", old_content)
        _run_git(["git", "checkout", "-b", "feature"], repo)
        _run_git(["git", "rm", "layered.txt"], repo)
        _run_git(["git", "commit", "-m", "delete oversized file"], repo)
        (repo / "layered.txt").write_text("safe replacement", encoding="utf-8")
        _run_git(["git", "add", "layered.txt"], repo)
        config = self._make_project_yml(
            "  layered_unsafe_deleted:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        manifest_page = mod.get_review_manifest(
            target="layered_unsafe_deleted", mode="A", cursor=""
        )
        manifest = json.loads(manifest_page["content"])
        page = mod.get_review_bundle_page(
            target="layered_unsafe_deleted", mode="A", cursor=""
        )
        self.assertIn(
            ("layered.txt", "oversized"),
            {(item["path"], item["reason"]) for item in manifest["omitted_files"]},
        )
        self.assertNotIn("old oversized content", page["content"])
        self.assertNotIn("safe replacement", page["content"])

    def test_nul_safe_filename_inventory_and_diff(self) -> None:
        repo = self._init_repo("nul_safe_names")
        path = "tab\tand\nnewline.txt"
        self._commit_file(repo, path, "before")
        _run_git(["git", "checkout", "-b", "feature"], repo)
        (repo / path).write_text("nul safe content", encoding="utf-8")
        config = self._make_project_yml(
            "  nul_safe_names:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        import json

        manifest_page = mod.get_review_manifest(
            target="nul_safe_names", mode="A", cursor=""
        )
        manifest = json.loads(manifest_page["content"])
        page = mod.get_review_bundle_page(target="nul_safe_names", mode="A", cursor="")
        self.assertEqual(manifest["changed_files"], [path])
        self.assertEqual(manifest["unstaged"], [path])
        self.assertIn("nul safe content", page["content"])

    def test_manifest_cursor_rejected_after_omitted_reason_changes(self) -> None:
        repo = self._init_repo("manifest_cursor_omitted")
        self._commit_file(repo, "base.txt", "base")
        (repo / "opaque.dat").write_bytes(b"\x00binary")
        config = (
            "limits:\n"
            "  max_file_bytes: 100\n"
            "  max_diff_chars: 120000\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 50\n"
            "deny_paths: []\n"
            "projects:\n"
            f"  manifest_cursor_omitted:\n"
            f"    source: {repo}\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        first_page = mod.get_review_manifest(
            target="manifest_cursor_omitted", mode="A", cursor=""
        )
        cursor = first_page["next_cursor"]
        self.assertIsNotNone(cursor)
        (repo / "opaque.dat").write_text("x" * 200, encoding="utf-8")
        with self.assertRaises(ValueError):
            mod.get_review_manifest(
                target="manifest_cursor_omitted", mode="A", cursor=cursor
            )

    def test_missing_host_raises_runtime_error(self) -> None:
        server_py = Path(__file__).resolve().parents[1] / "server.py"
        config_path = self.root / "_test_config_empty.yml"
        config_path.write_text(
            "limits:\n  max_file_bytes: 1\n  max_diff_chars: 1\n"
            "  max_context_chars: 1\n  max_tree_entries: 1\n"
            "  max_search_results: 1\n  max_review_page_chars: 1\n"
            "deny_paths: []\nprojects: {}\n",
            encoding="utf-8",
        )
        old = {
            k: os.environ.pop(k, None)
            for k in ("BEEMCP_HOST", "BEEMCP_PORT", "BEEMCP_CONFIG")
        }
        os.environ["BEEMCP_PORT"] = "8765"
        os.environ["BEEMCP_CONFIG"] = str(config_path)
        try:
            spec = importlib.util.spec_from_file_location("_test_no_host", server_py)
            if spec is None or spec.loader is None:
                raise AssertionError("Cannot load server.py")
            loader = spec.loader
            module = importlib.util.module_from_spec(spec)
            with self.assertRaises(RuntimeError) as ctx:
                loader.exec_module(module)
            self.assertIn("BEEMCP_HOST", str(ctx.exception))
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    def test_missing_port_raises_runtime_error(self) -> None:
        server_py = Path(__file__).resolve().parents[1] / "server.py"
        config_path = self.root / "_test_config_no_port.yml"
        config_path.write_text(
            "limits:\n  max_file_bytes: 1\n  max_diff_chars: 1\n"
            "  max_context_chars: 1\n  max_tree_entries: 1\n"
            "  max_search_results: 1\n  max_review_page_chars: 1\n"
            "deny_paths: []\nprojects: {}\n",
            encoding="utf-8",
        )
        old = {
            k: os.environ.pop(k, None)
            for k in ("BEEMCP_HOST", "BEEMCP_PORT", "BEEMCP_CONFIG")
        }
        os.environ["BEEMCP_HOST"] = "127.0.0.1"
        os.environ["BEEMCP_CONFIG"] = str(config_path)
        try:
            spec = importlib.util.spec_from_file_location("_test_no_port", server_py)
            if spec is None or spec.loader is None:
                raise AssertionError("Cannot load server.py")
            loader = spec.loader
            module = importlib.util.module_from_spec(spec)
            with self.assertRaises(RuntimeError) as ctx:
                loader.exec_module(module)
            self.assertIn("BEEMCP_PORT", str(ctx.exception))
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    def test_invalid_port_raises_runtime_error(self) -> None:
        server_py = Path(__file__).resolve().parents[1] / "server.py"
        config_path = self.root / "_test_config_bad_port.yml"
        config_path.write_text(
            "limits:\n  max_file_bytes: 1\n  max_diff_chars: 1\n"
            "  max_context_chars: 1\n  max_tree_entries: 1\n"
            "  max_search_results: 1\n  max_review_page_chars: 1\n"
            "deny_paths: []\nprojects: {}\n",
            encoding="utf-8",
        )
        old = {
            k: os.environ.pop(k, None)
            for k in ("BEEMCP_HOST", "BEEMCP_PORT", "BEEMCP_CONFIG")
        }
        os.environ["BEEMCP_HOST"] = "127.0.0.1"
        os.environ["BEEMCP_PORT"] = "99999"
        os.environ["BEEMCP_CONFIG"] = str(config_path)
        try:
            spec = importlib.util.spec_from_file_location("_test_bad_port", server_py)
            if spec is None or spec.loader is None:
                raise AssertionError("Cannot load server.py")
            loader = spec.loader
            module = importlib.util.module_from_spec(spec)
            with self.assertRaises(RuntimeError) as ctx:
                loader.exec_module(module)
            self.assertIn("65535", str(ctx.exception))
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    def test_non_numeric_port_raises_runtime_error(self) -> None:
        server_py = Path(__file__).resolve().parents[1] / "server.py"
        config_path = self.root / "_test_config_nan_port.yml"
        config_path.write_text(
            "limits:\n  max_file_bytes: 1\n  max_diff_chars: 1\n"
            "  max_context_chars: 1\n  max_tree_entries: 1\n"
            "  max_search_results: 1\n  max_review_page_chars: 1\n"
            "deny_paths: []\nprojects: {}\n",
            encoding="utf-8",
        )
        old = {
            k: os.environ.pop(k, None)
            for k in ("BEEMCP_HOST", "BEEMCP_PORT", "BEEMCP_CONFIG")
        }
        os.environ["BEEMCP_HOST"] = "127.0.0.1"
        os.environ["BEEMCP_PORT"] = "not-a-number"
        os.environ["BEEMCP_CONFIG"] = str(config_path)
        try:
            spec = importlib.util.spec_from_file_location("_test_nan_port", server_py)
            if spec is None or spec.loader is None:
                raise AssertionError("Cannot load server.py")
            loader = spec.loader
            module = importlib.util.module_from_spec(spec)
            with self.assertRaises(RuntimeError) as ctx:
                loader.exec_module(module)
            self.assertIn("integer", str(ctx.exception).lower())
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    def test_default_config_path_points_to_config_projects_yml(self) -> None:
        server_py = ROOT / "server.py"
        source = server_py.read_text(encoding="utf-8")
        self.assertIn('HERE / "config" / "projects.yml"', source)
        self.assertNotIn('HERE / "projects.yml"', source)

    def test_valid_host_port_passed_to_fastmcp(self) -> None:
        repo = self._init_repo("valid_host_port")
        self._commit_file(repo, "f.txt", "x")
        config = self._make_project_yml(
            "  valid_host_port:\n"
            "    source: " + str(repo) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        mod = self._import_server(config)
        self.assertEqual(mod.MCP_HOST, "127.0.0.1")
        self.assertEqual(mod.MCP_PORT, 8765)
        # Verify tools still load correctly
        tools = mod.mcp._tool_manager.list_tools()
        self.assertEqual(len(tools), 9)


class StreamableHttpIntegrationTests(unittest.TestCase):
    def test_review_cursor_round_trip_uses_streamable_http_transport(self) -> None:
        import socket

        import uvicorn
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        if TMP_ROOT is None:
            self.skipTest("no writable temporary storage")
        server: uvicorn.Server | None = None
        thread: threading.Thread | None = None
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temp:
            root = Path(temp)
            repo = root / "review-repo"
            repo.mkdir()
            _run_git(["git", "init", "-b", "main"], repo)
            _run_git(["git", "config", "user.email", "test@test"], repo)
            _run_git(["git", "config", "user.name", "Test"], repo)
            (repo / "base.txt").write_text("base\n", encoding="utf-8")
            _run_git(["git", "add", "base.txt"], repo)
            _run_git(["git", "commit", "-m", "base"], repo)
            _run_git(["git", "checkout", "-b", "feature"], repo)
            (repo / "large.txt").write_text(
                "TRANSPORT_CURSOR_TOKEN\n" + ("x" * 80 + "\n") * 20,
                encoding="utf-8",
            )
            _run_git(["git", "add", "large.txt"], repo)
            _run_git(["git", "commit", "-m", "large diff"], repo)
            config = root / "projects.yml"
            config.write_text(
                "limits:\n"
                "  max_file_bytes: 200000\n"
                "  max_diff_chars: 120000\n"
                "  max_context_chars: 160000\n"
                "  max_tree_entries: 4000\n"
                "  max_search_results: 200\n"
                "  max_review_page_chars: 100\n"
                "deny_paths: []\n"
                "projects:\n"
                f"  transport-proj:\n    source: {repo}\n    base_branch: main\n"
                "    context_modes:\n      A:\n        files: []\n",
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
                    "transport_cursor_server", ROOT / "server.py"
                )
                if spec is None or spec.loader is None:
                    raise AssertionError("Cannot load server.py")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                server = uvicorn.Server(
                    uvicorn.Config(
                        module.mcp.streamable_http_app(),
                        host="127.0.0.1",
                        port=port,
                        log_level="error",
                    )
                )
                thread = threading.Thread(target=server.run, daemon=True)
                thread.start()
                deadline = time.monotonic() + 5
                while not server.started and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(server.started)

                async def call_pages() -> tuple[
                    dict[str, object], dict[str, object], str, dict[str, object]
                ]:
                    async with streamable_http_client(
                        f"http://127.0.0.1:{port}/mcp"
                    ) as streams:
                        read_stream, write_stream, _ = streams
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            first = await session.call_tool(
                                "get_review_bundle_page",
                                arguments={
                                    "target": "transport-proj",
                                    "mode": "A",
                                    "cursor": "",
                                },
                            )
                            first_data = dict(first.structuredContent or {})
                            cursor = first_data["next_cursor"]
                            self.assertIsInstance(cursor, str)
                            second = await session.call_tool(
                                "get_review_bundle_page",
                                arguments={
                                    "target": "transport-proj",
                                    "mode": "A",
                                    "cursor": cursor,
                                },
                            )
                            decoded = module._decode_diff_cursor(
                                cursor,
                                "transport-proj",
                                "A",
                                first_data["snapshot_id"],
                            )
                            for length in range(4):
                                legacy_cursor = base64.urlsafe_b64encode(
                                    json.dumps(
                                        {
                                            "kind": "diff",
                                            "target": "transport-proj",
                                            "mode": "A",
                                            "snapshot_id": first_data["snapshot_id"],
                                            **decoded,
                                            "legacy": "x" * length,
                                        },
                                        sort_keys=True,
                                    ).encode("utf-8")
                                ).decode("ascii")
                                if legacy_cursor.endswith(
                                    "="
                                ) and not legacy_cursor.endswith("=="):
                                    break
                            else:
                                raise AssertionError("could not create padded cursor")
                            legacy = await session.call_tool(
                                "get_review_bundle_page",
                                arguments={
                                    "target": "transport-proj",
                                    "mode": "A",
                                    "cursor": legacy_cursor,
                                },
                            )
                            return (
                                first_data,
                                dict(second.structuredContent or {}),
                                legacy_cursor,
                                dict(legacy.structuredContent or {}),
                            )

                first, second, legacy_cursor, legacy = asyncio.run(call_pages())
                self.assertTrue(first["has_more"])
                next_cursor = first["next_cursor"]
                self.assertIsInstance(next_cursor, str)
                assert isinstance(next_cursor, str)
                self.assertNotIn("=", next_cursor)
                self.assertEqual(second["cursor"], first["next_cursor"])
                self.assertEqual(second["page_number"], 1)
                self.assertTrue(legacy_cursor.endswith("="))
                self.assertEqual(legacy["cursor"], legacy_cursor)
                self.assertEqual(legacy["page_number"], 1)
                self.assertIn(
                    "TRANSPORT_CURSOR_TOKEN",
                    str(first["content"]) + str(second["content"]),
                )
            finally:
                if server is not None:
                    server.should_exit = True
                if thread is not None:
                    thread.join(timeout=5)
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

    def test_live_tools_list_uses_streamable_http_lifecycle(self) -> None:
        import socket

        import uvicorn
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        if TMP_ROOT is None:
            self.skipTest("no writable temporary storage")
        server: uvicorn.Server | None = None
        thread: threading.Thread | None = None
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
                f"  test-proj:\n    source: {ROOT}\n    base_branch: main\n"
                "    context_modes:\n      A:\n        files: []\n",
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
                    "transport_server", ROOT / "server.py"
                )
                if spec is None or spec.loader is None:
                    raise AssertionError("Cannot load server.py")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                server = uvicorn.Server(
                    uvicorn.Config(
                        module.mcp.streamable_http_app(),
                        host="127.0.0.1",
                        port=port,
                        log_level="error",
                    )
                )
                thread = threading.Thread(target=server.run, daemon=True)
                thread.start()
                deadline = time.monotonic() + 5
                while not server.started and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(server.started)

                async def list_names() -> set[str]:
                    async with streamable_http_client(
                        f"http://127.0.0.1:{port}/mcp"
                    ) as streams:
                        read_stream, write_stream, _ = streams
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            result = await session.list_tools()
                    return {tool.name for tool in result.tools}

                names = asyncio.run(list_names())
                self.assertEqual(names, set(module.EXPECTED_TOOLS))
            finally:
                if server is not None:
                    server.should_exit = True
                if thread is not None:
                    thread.join(timeout=5)
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


if __name__ == "__main__":
    unittest.main()
