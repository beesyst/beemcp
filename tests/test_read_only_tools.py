from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


TMP_ROOT: Path | None = None

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
            '  - ".github/workflows/**"\n'
            "projects:\n" + projects_config
        )

    def _import_server(self, config_text: str):
        import importlib.util
        server_py = Path(__file__).resolve().parents[1] / "server.py"
        config_path = self.root / "_test_config.yml"
        config_path.write_text(config_text, encoding="utf-8")
        old = os.environ.get("BEE_MCP_CONFIG")
        os.environ["BEE_MCP_CONFIG"] = str(config_path)
        try:
            spec = importlib.util.spec_from_file_location("_test_server", server_py)
            if spec is None or spec.loader is None:
                raise AssertionError("Cannot load server.py")
            loader = spec.loader
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            return module
        finally:
            if old is None:
                os.environ.pop("BEE_MCP_CONFIG", None)
            else:
                os.environ["BEE_MCP_CONFIG"] = old

    # --- Tests ---

    def test_list_projects_returns_allowlisted_projects_and_modes(self) -> None:
        repo = self._init_repo("proj_a")
        self._commit_file(repo, "README.md")
        config = self._make_project_yml(
            '  proj_a:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files:\n'
            '          - README.md\n'
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

    def test_list_worktrees_detects_main_and_linked(self) -> None:
        main_repo = self._init_repo("wt_main")
        self._commit_file(main_repo, "file.txt")
        wt_path = self.root / "wt_linked"
        _run_git(["git", "worktree", "add", "--detach", str(wt_path), "main"], main_repo)
        config = self._make_project_yml(
            '  wt_main:\n'
            '    source: ' + str(main_repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        result = mod.list_worktrees(project="wt_main")
        self.assertEqual(result["project"], "wt_main")
        targets = [w["target"] for w in result["worktrees"]]
        self.assertIn("wt_main", targets)
        self.assertIn(wt_path.name, targets)

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
            '  wt_base:\n'
            '    source: ' + str(main_repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
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
            '  wto_a:\n'
            '    source: ' + str(repo_a) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        with self.assertRaises(ValueError):
            mod._resolve_target("wto_b")

    def test_unknown_target_raises_value_error(self) -> None:
        repo = self._init_repo("known_only")
        self._commit_file(repo, "x.txt")
        config = self._make_project_yml(
            '  known_only:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        with self.assertRaises(ValueError):
            mod._resolve_target("nonexistent_target")

    def test_mode_A_reads_only_configured_files(self) -> None:
        repo = self._init_repo("mode_a")
        self._commit_file(repo, "README.md", "# Readme")
        self._commit_file(repo, "SECRET.txt", "shh")
        config = self._make_project_yml(
            '  mode_a:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files:\n'
            '          - README.md\n'
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
            '  mode_b:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files:\n'
            '          - README.md\n'
            '      B:\n'
            '        extends: A\n'
            '        files:\n'
            '          - config.yml\n'
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
            '  mode_c:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files:\n'
            '          - README.md\n'
            '      C:\n'
            '        extends: A\n'
            '        tree_roots:\n'
            '          - src\n'
        )
        mod = self._import_server(config)
        result = mod.get_project_context(target="mode_c", mode="C")
        self.assertIn("README.md", result["files"])
        self.assertIn("repository_tree", result)
        tree = result["repository_tree"]
        self.assertIn("src/main.py", tree)
        self.assertIn("src/util.py", tree)
        self.assertNotIn("docs/guide.md", tree)

    def test_related_roadmap_returned(self) -> None:
        proj_a = self._init_repo("rel_a")
        self._commit_file(proj_a, "docs/ROADMAP.md", "# Roadmap A")
        proj_b = self._init_repo("rel_b")
        self._commit_file(proj_b, "docs/ROADMAP.md", "# Roadmap B")
        config = self._make_project_yml(
            '  rel_a:\n'
            '    source: ' + str(proj_a) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files:\n'
            '          - docs/ROADMAP.md\n'
            '    related:\n'
            '      - project: rel_b\n'
            '        files:\n'
            '          - docs/ROADMAP.md\n'
            '  rel_b:\n'
            '    source: ' + str(proj_b) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
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
            '  rb_commit:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="rb_commit", mode="A")
        self.assertIn("feature.txt", result["changed_files"])

    def test_bundle_includes_staged_changes(self) -> None:
        repo = self._init_repo("rb_staged")
        self._commit_file(repo, "base.txt", "base")
        (repo / "staged.txt").write_text("staged", encoding="utf-8")
        _run_git(["git", "add", "staged.txt"], repo)
        config = self._make_project_yml(
            '  rb_staged:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="rb_staged", mode="A")
        self.assertIn("staged.txt", result["changed_files"])

    def test_bundle_includes_unstaged_changes(self) -> None:
        repo = self._init_repo("rb_unstaged")
        self._commit_file(repo, "base.txt", "base")
        (repo / "base.txt").write_text("modified", encoding="utf-8")
        config = self._make_project_yml(
            '  rb_unstaged:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="rb_unstaged", mode="A")
        self.assertIn("base.txt", result["changed_files"])

    def test_bundle_includes_safe_untracked_text_files(self) -> None:
        repo = self._init_repo("rb_untracked")
        self._commit_file(repo, "base.txt", "base")
        (repo / "new_file.txt").write_text("untracked", encoding="utf-8")
        config = self._make_project_yml(
            '  rb_untracked:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="rb_untracked", mode="A")
        self.assertIn("new_file.txt", result["untracked_files"])

    def test_bundle_marks_deleted_file(self) -> None:
        repo = self._init_repo("rb_deleted")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, "todelete.txt", "delete me")
        _run_git(["git", "checkout", "-b", "delete-branch"], repo)
        _run_git(["git", "rm", "todelete.txt"], repo)
        _run_git(["git", "commit", "-m", "delete todelete.txt"], repo)
        config = self._make_project_yml(
            '  rb_deleted:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="rb_deleted", mode="A")
        self.assertIn("todelete.txt", result["deleted_files"])
        self.assertIn("todelete.txt", result["diff"])

    def test_bundle_marks_rename(self) -> None:
        repo = self._init_repo("rb_rename")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, "old.txt", "rename me")
        _run_git(["git", "checkout", "-b", "rename-branch"], repo)
        _run_git(["git", "mv", "old.txt", "new.txt"], repo)
        _run_git(["git", "commit", "-m", "rename old to new"], repo)
        config = self._make_project_yml(
            '  rb_rename:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="rb_rename", mode="A")
        self.assertTrue(
            any(
                r["from"] == "old.txt" and r["to"] == "new.txt"
                for r in result["renamed_files"]
            ),
            msg=f"No rename old.txt->new.txt in {result['renamed_files']}",
        )

    def test_bundle_returns_current_changed_text_file_contents(self) -> None:
        repo = self._init_repo("rb_contents")
        self._commit_file(repo, "base.txt", "base")
        (repo / "base.txt").write_text("updated content", encoding="utf-8")
        config = self._make_project_yml(
            '  rb_contents:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="rb_contents", mode="A")
        self.assertIn("base.txt", result["file_contents"])
        self.assertEqual(result["file_contents"]["base.txt"], "updated content")

    def test_untracked_env_absent_from_diff_and_file_contents(self) -> None:
        repo = self._init_repo("rb_deny")
        self._commit_file(repo, "base.txt", "base")
        (repo / ".env").write_text("SECRET=key", encoding="utf-8")
        config = self._make_project_yml(
            '  rb_deny:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="rb_deny", mode="A")
        self.assertNotIn(".env", result["file_contents"])
        self.assertIn(".env", result["redacted_paths"])
        self.assertNotIn(".env", result.get("diff", ""))

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
            '  rb_nest_env:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="rb_nest_env", mode="A")
        self.assertNotIn("sub/.env", result["file_contents"])
        self.assertIn("sub/.env", result["redacted_paths"])
        self.assertNotIn("sub/.env", result.get("diff", ""))

    def test_untracked_alias_to_env_not_resolved(self) -> None:
        repo = self._init_repo("rb_alias")
        self._commit_file(repo, "base.txt", "base")
        (repo / ".env").write_text("SECRET=key", encoding="utf-8")
        (repo / "alias.txt").symlink_to(".env")
        config = self._make_project_yml(
            '  rb_alias:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="rb_alias", mode="A")
        self.assertNotIn("alias.txt", result["file_contents"])
        self.assertNotIn("SECRET=key", result.get("diff", ""))

    def test_untracked_escaping_symlink_not_read(self) -> None:
        repo = self._init_repo("rb_escape")
        self._commit_file(repo, "base.txt", "base")
        escape_target = self.root / "outside.txt"
        escape_target.write_text("escaped", encoding="utf-8")
        (repo / "escape.txt").symlink_to(os.path.relpath(escape_target, repo))
        config = self._make_project_yml(
            '  rb_escape:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="rb_escape", mode="A")
        self.assertNotIn("escape.txt", result["file_contents"])
        self.assertNotIn("escaped", result.get("diff", ""))

    def test_broken_symlink_not_read(self) -> None:
        repo = self._init_repo("rb_broken")
        self._commit_file(repo, "base.txt", "base")
        (repo / "broken.txt").symlink_to("nonexistent")
        config = self._make_project_yml(
            '  rb_broken:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="rb_broken", mode="A")
        self.assertNotIn("broken.txt", result["file_contents"])
        self.assertNotIn("broken.txt", result.get("diff", ""))

    def test_tracked_binary_file_not_in_diff(self) -> None:
        repo = self._init_repo("rb_binary")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "binary-branch"], repo)
        (repo / "data.bin").write_bytes(b"\x00\x01\x02")
        _run_git(["git", "add", "data.bin"], repo)
        _run_git(["git", "commit", "-m", "add binary"], repo)
        config = self._make_project_yml(
            '  rb_binary:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="rb_binary", mode="A")
        self.assertNotIn("data.bin", result["file_contents"])
        self.assertNotIn("data.bin", result.get("diff", ""))

    def test_tracked_oversized_file_not_in_diff(self) -> None:
        repo = self._init_repo("rb_oversized")
        self._commit_file(repo, "base.txt", "base")
        _run_git(["git", "checkout", "-b", "oversized-branch"], repo)
        large = "x" * 300000
        (repo / "large.txt").write_text(large, encoding="utf-8")
        _run_git(["git", "add", "large.txt"], repo)
        _run_git(["git", "commit", "-m", "add large"], repo)
        config = self._make_project_yml(
            '  rb_oversized:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="rb_oversized", mode="A")
        self.assertNotIn("large.txt", result["file_contents"])
        self.assertNotIn("large.txt", result.get("diff", ""))
        omitted_paths = {o["path"] for o in result["omitted_files"]}
        self.assertIn("large.txt", omitted_paths,
                      msg=f"large.txt not in omitted: {result['omitted_files']}")

    def test_read_project_file_rejects_absolute_path(self) -> None:
        repo = self._init_repo("rpf_abs")
        self._commit_file(repo, "safe.txt", "safe")
        config = self._make_project_yml(
            '  rpf_abs:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        with self.assertRaises((ValueError, RuntimeError)):
            mod.read_project_file(target="rpf_abs", path="/etc/passwd")

    def test_read_project_file_rejects_dot_dot(self) -> None:
        repo = self._init_repo("rpf_dotdot")
        self._commit_file(repo, "safe.txt", "safe")
        config = self._make_project_yml(
            '  rpf_dotdot:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        with self.assertRaises((ValueError, RuntimeError)):
            mod.read_project_file(target="rpf_dotdot", path="../outside.txt")

    def test_search_project_fixed_string(self) -> None:
        repo = self._init_repo("search_fixed")
        self._commit_file(repo, "src/main.py", "def hello(): pass")
        self._commit_file(repo, "README.md", "# Hello World")
        config = self._make_project_yml(
            '  search_fixed:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files:\n'
            '          - src/main.py\n'
            '          - README.md\n'
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
            '  search_val:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files:\n'
            '          - f.txt\n'
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
            '  valid_proj:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
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
            '  ro_check:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        tools = mod.mcp._tool_manager.list_tools()
        for t in tools:
            self.assertIsInstance(
                t.annotations, ToolAnnotations,
                f"Tool {t.name} annotations is not ToolAnnotations"
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
            '  no_agent:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        tools = mod.mcp._tool_manager.list_tools()
        tool_names = [t.name for t in tools]
        forbidden = [
            "prepare_run", "start_copilot_initial", "start_codex_fix",
            "get_job", "get_diff", "read_changed_file", "run_checks",
            "discard_run",
        ]
        for name in forbidden:
            self.assertNotIn(name, tool_names, f"Forbidden tool found: {name}")

    def test_health_readiness_routes_work(self) -> None:
        import asyncio
        repo = self._init_repo("health_test")
        self._commit_file(repo, "f.txt", "x")
        config = self._make_project_yml(
            '  health_test:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
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

    def test_module_imports_without_tmpdir(self) -> None:
        import importlib.util
        server_py = Path(__file__).resolve().parents[1] / "server.py"
        spec = importlib.util.spec_from_file_location("_server_import", server_py)
        if spec is None or spec.loader is None:
            raise AssertionError("Cannot load server.py for import test")
        loader = spec.loader
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        self.assertTrue(hasattr(module, "list_projects"))
        self.assertTrue(hasattr(module, "get_review_bundle"))

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
            '  rel_main:\n'
            '    source: ' + str(main_repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files:\n'
            '          - docs/guide.md\n'
            '    related:\n'
            '      - project: rel_sec\n'
            '        files:\n'
            '          - docs/config.txt\n'
            '  rel_sec:\n'
            '    source: ' + str(related_repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
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
            "  max_diff_chars: 500000\n"
            "  max_context_chars: 5000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
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
            '  mg_fail:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
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
            '  rb_del_deny:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="rb_del_deny", mode="A")
        self.assertIn(".env", result["deleted_files"])
        self.assertNotIn(".env", result.get("diff", ""))
        self.assertIn(".env", result["redacted_paths"])

    def test_shared_context_budget_files_and_related(self) -> None:
        main_repo = self._init_repo("shared_ctx")
        self._commit_file(main_repo, "a.txt", "a" * 3000)
        self._commit_file(main_repo, "b.txt", "b" * 3000)
        related_repo = self._init_repo("shared_ctx_rel")
        self._commit_file(related_repo, "r.txt", "r" * 3000)
        config = (
            "limits:\n"
            "  max_file_bytes: 500000\n"
            "  max_diff_chars: 500000\n"
            "  max_context_chars: 5000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
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
            '  stg_ren:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="stg_ren", mode="A")
        self.assertTrue(
            any(
                r["from"] == "old.txt" and r["to"] == "new.txt"
                for r in result["renamed_files"]
            ),
            msg=f"No staged rename in {result['renamed_files']}",
        )
        self.assertIn("old.txt", result["diff"])
        self.assertIn("new.txt", result["diff"])

    def test_staged_rename_both_paths_in_diff(self) -> None:
        repo = self._init_repo("stg_ren2")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, "old.txt", "rename me")
        _run_git(["git", "mv", "old.txt", "new.txt"], repo)
        config = self._make_project_yml(
            '  stg_ren2:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="stg_ren2", mode="A")
        self.assertTrue(
            any(
                r["from"] == "old.txt" and r["to"] == "new.txt"
                for r in result["renamed_files"]
            ),
            msg=f"No staged rename in {result['renamed_files']}",
        )
        self.assertIn("old.txt", result["diff"])
        self.assertIn("new.txt", result["diff"])

    def test_rename_from_denied_path_not_leaked(self) -> None:
        repo = self._init_repo("den_ren")
        self._commit_file(repo, "base.txt", "base")
        self._commit_file(repo, ".env", "SECRET=key")
        _run_git(["git", "checkout", "-b", "den-ren"], repo)
        _run_git(["git", "mv", ".env", "config.env"], repo)
        _run_git(["git", "commit", "-m", "rename .env to config.env"], repo)
        config = self._make_project_yml(
            '  den_ren:\n'
            '    source: ' + str(repo) + '\n'
            '    base_branch: main\n'
            '    context_modes:\n'
            '      A:\n'
            '        files: []\n'
        )
        mod = self._import_server(config)
        result = mod.get_review_bundle(target="den_ren", mode="A")
        self.assertNotIn("SECRET=key", result.get("diff", ""))
        self.assertNotIn("config.env", result.get("file_contents", {}))
        self.assertTrue(
            any(".env" in p for p in result.get("redacted_paths", [])),
            msg=f".env not in redacted_paths: {result.get('redacted_paths')}",
        )
        self.assertTrue(
            any("config.env" in p for p in result.get("redacted_paths", [])),
            msg=f"config.env not in redacted_paths: {result.get('redacted_paths')}",
        )

    def _chunk_config(self, repo: Path, name: str,
                      max_file_bytes: int = 100,
                      max_context_chars: int = 200) -> str:
        return (
            "limits:\n"
            f"  max_file_bytes: {max_file_bytes}\n"
            "  max_diff_chars: 120000\n"
            f"  max_context_chars: {max_context_chars}\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
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
            r = mod.read_project_file(target="rpf_seq", path="big.txt",
                                      start_line=sl, start_column=sc)
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
        sr = mod.search_project(target="srch_ovr", query="SEARCH_ME",
                                mode="A")
        self.assertTrue(len(sr["results"]) >= 1,
                        msg="should find matches in oversized file")
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
            r = mod.read_project_file(target="rpf_longln", path="long.txt",
                                      start_line=sl, start_column=sc)
            self.assertLessEqual(len(r["content"]), 100,
                                 msg="chunk exceeds max_context_chars")
            chunks.append(r["content"])
            if r["next_line"] is None and r["next_column"] is None:
                break
            self.assertTrue(
                r["next_line"] != sl or r["next_column"] != sc,
                msg="cursor did not advance",
            )
            sl, sc = r["next_line"], r["next_column"]

        self.assertEqual("".join(chunks), text,
                         msg="reassembled content must match original")

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
        self.assertIn("context_limit", reasons,
                      msg="context_limit reason must be present")
        self.assertIn("b.txt", paths,
                      msg="b.txt should be omitted (budget exhausted)")

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
        self.assertTrue(len(related_omit) >= 1,
                        msg="should have at least one related omitted entry")
        found = any(
            o["target"] == "rel_omit_rel" and o["path"] == "big.txt"
            for o in related_omit
        )
        self.assertTrue(found,
                        msg="rel_omit_rel/big.txt should be in related_omitted")

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
        self.assertLessEqual(total, 500,
                             msg=f"total search response too large: {total}")

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
        result = mod.get_review_bundle(target="rb_omit_ctx", mode="A")

        self.assertIn("omitted_files", result)
        self.assertIn("related_omitted_files", result)
        omitted_paths = {o["path"] for o in result["omitted_files"]}
        self.assertIn("docs/ROADMAP.md", omitted_paths,
                      msg="oversized own ROADMAP should be in omitted_files")
        related_omit_paths = {o["path"] for o in result["related_omitted_files"]}
        self.assertIn("docs/ROADMAP.md", related_omit_paths,
                      msg="oversized related ROADMAP should be in related_omitted_files")
        related_targets = {o["target"] for o in result["related_omitted_files"]}
        self.assertIn("rb_omit_rel", related_targets,
                      msg="related target should be rb_omit_rel")

    def test_search_serialized_size_respected(self) -> None:
        repo = self._init_repo("srch_ser")
        lines = [
            'simple line\n',
            'line with "quotes" and \\backslash\n',
            'line with unicode: \u00e9\u00e0\u00fc\u00f1\n',
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
        self.assertTrue(len(sr["results"]) > 0,
                        msg="search should find matches")
        serialized = json.dumps(sr, ensure_ascii=False)
        self.assertLessEqual(
            len(serialized), 200,
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
        result = mod.get_review_bundle(
            target="rb_rename_oversized",
            mode="A",
        )

        self.assertTrue(
            any(
                rename["from"] == "old.txt"
                and rename["to"] == "new.txt"
                for rename in result["renamed_files"]
            )
        )
        self.assertNotIn("old.txt", result["diff"])
        self.assertNotIn("new.txt", result["diff"])
        self.assertTrue(
            any(
                item["path"] == "new.txt"
                and item["reason"] == "oversized"
                for item in result["omitted_files"]
            )
        )


if __name__ == "__main__":
    unittest.main()
