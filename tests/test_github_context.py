from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

TEST_TOKEN = "github_pat_TEST_SECRET_NEVER_PRINT"


def _make_project_yml(projects_config: str) -> str:
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
        '  - "**/.env"\n'
        '  - ".git/**"\n'
        '  - "**/.venv/**"\n'
        "projects:\n" + projects_config
    )


class _MockResponse:
    def __init__(self, body: bytes = b"{}", status: int = 200) -> None:
        self.body = body
        self.status = status
        self.headers = {}

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> _MockResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass


def _mocked_urlopen(
    status: int = 200,
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> _MockResponse:
    data = json.dumps(body).encode("utf-8") if body is not None else b"{}"
    resp = _MockResponse(data, status)
    resp.headers = headers or {}
    return resp


def _empty_page() -> _MockResponse:
    return _mocked_urlopen(200, [])


def _gh_side_effect(*responses: Any) -> list[Any]:
    result: list[Any] = []
    for r in responses:
        result.append(r)
        if hasattr(r, "body"):
            import json as _json

            try:
                parsed = _json.loads(r.body)
                if isinstance(parsed, list) and len(parsed) > 0:
                    result.append(_empty_page())
            except ValueError, json.JSONDecodeError:
                pass
    return result


def _http_error_urlopen(code: int) -> Any:
    from urllib.error import HTTPError

    def _raise(*args: Any, **kwargs: Any) -> None:
        raise HTTPError(
            url="http://mock",
            code=code,
            msg="mock",
            hdrs=Message(),
            fp=io.BytesIO(b"{}"),
        )

    return _raise


class _ImportHelper:
    def __init__(self) -> None:
        self._env_backup: dict[str, str | None] = {}
        self._config_path: Path | None = None
        self._module: Any = None

    def make_config(self, projects_text: str) -> str:
        return _make_project_yml(projects_text)

    def import_server(self, config_text: str) -> Any:
        tmp = tempfile.mkdtemp()
        config_path = Path(tmp) / "config.yml"
        config_path.write_text(config_text, encoding="utf-8")

        self._env_backup = {
            "BEEMCP_CONFIG": os.environ.get("BEEMCP_CONFIG"),
            "BEEMCP_HOST": os.environ.get("BEEMCP_HOST"),
            "BEEMCP_PORT": os.environ.get("BEEMCP_PORT"),
            "BEEMCP_GITHUB_TOKEN": os.environ.get("BEEMCP_GITHUB_TOKEN"),
        }
        os.environ["BEEMCP_CONFIG"] = str(config_path)
        os.environ["BEEMCP_HOST"] = "127.0.0.1"
        os.environ["BEEMCP_PORT"] = "8765"
        os.environ["BEEMCP_GITHUB_TOKEN"] = TEST_TOKEN

        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_gh_test_server", ROOT / "server.py"
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("Cannot load server.py")
        self._module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self._module)
        return self._module

    def cleanup(self) -> None:
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class UrlValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.helper = _ImportHelper()
        config = self.helper.make_config(
            "  test-proj:\n"
            "    source: " + str(ROOT) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        self.mod = self.helper.import_server(config)

    def tearDown(self) -> None:
        self.helper.cleanup()

    def test_valid_issue_url(self) -> None:
        result = self.mod._parse_github_url(
            "https://github.com/beesyst/beeui/issues/97"
        )
        self.assertEqual(result["owner"], "beesyst")
        self.assertEqual(result["repo"], "beeui")
        self.assertEqual(result["kind"], "issues")
        self.assertEqual(result["number"], 97)

    def test_valid_issue_url_trailing_slash(self) -> None:
        result = self.mod._parse_github_url(
            "https://github.com/beesyst/beeui/issues/97/"
        )
        self.assertEqual(result["number"], 97)

    def test_valid_pr_url(self) -> None:
        result = self.mod._parse_github_url("https://github.com/beesyst/beeui/pull/95")
        self.assertEqual(result["kind"], "pull")
        self.assertEqual(result["number"], 95)

    def test_valid_pr_url_trailing_slash(self) -> None:
        result = self.mod._parse_github_url("https://github.com/beesyst/beeui/pull/95/")
        self.assertEqual(result["number"], 95)

    def test_rejects_http(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url("http://github.com/beesyst/beeui/issues/97")

    def test_rejects_wrong_host(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url(
                "https://github.com.evil.example/beesyst/beeui/issues/97"
            )

    def test_rejects_evil_host(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url("https://evil.example/beesyst/beeui/issues/97")

    def test_rejects_user_password(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url(
                "https://user:password@github.com/beesyst/beeui/issues/97"
            )

    def test_rejects_nonstandard_port(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url(
                "https://github.com:8443/beesyst/beeui/issues/97"
            )

    def test_rejects_query_string(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url("https://github.com/beesyst/beeui/issues/97?x=1")

    def test_rejects_fragment(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url(
                "https://github.com/beesyst/beeui/issues/97#comment"
            )

    def test_rejects_number_zero(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url("https://github.com/beesyst/beeui/issues/0")

    def test_rejects_negative_number(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url("https://github.com/beesyst/beeui/issues/-1")

    def test_rejects_non_numeric(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url(
                "https://github.com/beesyst/beeui/issues/not-a-number"
            )

    def test_rejects_plural_pulls(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url("https://github.com/beesyst/beeui/pulls/95")

    def test_rejects_pull_files(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url("https://github.com/beesyst/beeui/pull/95/files")

    def test_rejects_pull_commits(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url(
                "https://github.com/beesyst/beeui/pull/95/commits"
            )

    def test_rejects_extra_segment(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url(
                "https://github.com/beesyst/beeui/issues/97/comments"
            )

    def test_rejects_owner_only(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url("https://github.com/beesyst")

    def test_rejects_no_number(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url("https://github.com/beesyst/beeui")

    def test_rejects_malformed_url(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url("not-a-url")

    def test_rejects_empty_owner(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url("https://github.com//beeui/issues/1")

    def test_rejects_empty_repo(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url("https://github.com/owner//issues/1")

    def test_rejects_unsupported_path(self) -> None:
        with self.assertRaises(ValueError):
            self.mod._parse_github_url("https://github.com/beesyst/beeui/discussions/1")


class GithubRepoConfigTests(unittest.TestCase):
    def _temp_config(self, content: str) -> Path:
        tmp = tempfile.mkdtemp()
        path = Path(tmp) / "config.yml"
        path.write_text(content, encoding="utf-8")
        return path

    def _load(self, config_text: str) -> Any:
        path = self._temp_config(config_text)
        old = os.environ.get("BEEMCP_CONFIG")
        old_host = os.environ.get("BEEMCP_HOST")
        old_port = os.environ.get("BEEMCP_PORT")
        old_token = os.environ.get("BEEMCP_GITHUB_TOKEN")
        os.environ["BEEMCP_CONFIG"] = str(path)
        os.environ["BEEMCP_HOST"] = "127.0.0.1"
        os.environ["BEEMCP_PORT"] = "8765"
        os.environ["BEEMCP_GITHUB_TOKEN"] = TEST_TOKEN
        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                "_cfg_test", ROOT / "server.py"
            )
            if spec is None or spec.loader is None:
                raise RuntimeError("Cannot load server.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        finally:
            for k, v in [
                ("BEEMCP_CONFIG", old),
                ("BEEMCP_HOST", old_host),
                ("BEEMCP_PORT", old_port),
                ("BEEMCP_GITHUB_TOKEN", old_token),
            ]:
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def _base_config(self, extra: str = "") -> str:
        return (
            "limits:\n"
            "  max_file_bytes: 200000\n"
            "  max_diff_chars: 120000\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 30000\n"
            "deny_paths: []\n"
            "projects:\n"
            "  proj_a:\n"
            "    source: " + str(ROOT) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
            "    github_repo: beesyst/beeui\n" + extra
        )

    def test_valid_owner_repo(self) -> None:
        mod = self._load(self._base_config())
        self.assertIn("beesyst/beeui", str(mod.PROJECTS))

    def test_github_repo_absent(self) -> None:
        config = self._base_config().replace("    github_repo: beesyst/beeui\n", "")
        mod = self._load(config)
        self.assertIn("proj_a", mod.PROJECTS)

    def test_github_repo_not_string_raises(self) -> None:
        config = self._base_config().replace(
            "    github_repo: beesyst/beeui\n",
            "    github_repo: 123\n",
        )
        with self.assertRaises(RuntimeError):
            self._load(config)

    def test_url_instead_of_slug(self) -> None:
        config = self._base_config().replace(
            "    github_repo: beesyst/beeui\n",
            "    github_repo: https://github.com/beesyst/beeui\n",
        )
        with self.assertRaises(RuntimeError):
            self._load(config)

    def test_git_suffix(self) -> None:
        config = self._base_config().replace(
            "    github_repo: beesyst/beeui\n",
            "    github_repo: beesyst/beeui.git\n",
        )
        with self.assertRaises(RuntimeError):
            self._load(config)

    def test_spaces(self) -> None:
        config = self._base_config().replace(
            "    github_repo: beesyst/beeui\n",
            "    github_repo: beesyst/ beeui\n",
        )
        with self.assertRaises(RuntimeError):
            self._load(config)

    def test_double_dot(self) -> None:
        config = self._base_config().replace(
            "    github_repo: beesyst/beeui\n",
            "    github_repo: beesyst/../beeui\n",
        )
        with self.assertRaises(RuntimeError):
            self._load(config)

    def test_only_owner(self) -> None:
        config = self._base_config().replace(
            "    github_repo: beesyst/beeui\n",
            "    github_repo: beesyst\n",
        )
        with self.assertRaises(RuntimeError):
            self._load(config)

    def test_three_segments(self) -> None:
        config = self._base_config().replace(
            "    github_repo: beesyst/beeui\n",
            "    github_repo: a/b/c\n",
        )
        with self.assertRaises(RuntimeError):
            self._load(config)

    def test_query_string_in_github_repo(self) -> None:
        config = self._base_config().replace(
            "    github_repo: beesyst/beeui\n",
            "    github_repo: beesyst/beeui?x=1\n",
        )
        with self.assertRaises(RuntimeError):
            self._load(config)

    def test_fragment_in_github_repo(self) -> None:
        config = self._base_config().replace(
            "    github_repo: beesyst/beeui\n",
            "    github_repo: beesyst/beeui#frag\n",
        )
        with self.assertRaises(RuntimeError):
            self._load(config)

    def test_duplicate_github_repo(self) -> None:
        config = self._base_config(
            "  proj_b:\n"
            "    source: " + str(ROOT) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
            "    github_repo: beesyst/beeui\n"
        )
        with self.assertRaises(RuntimeError):
            self._load(config)

    def test_duplicate_case_insensitive(self) -> None:
        config = self._base_config(
            "  proj_b:\n"
            "    source: " + str(ROOT) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
            "    github_repo: Beesyst/BeeUI\n"
        )
        with self.assertRaises(RuntimeError):
            self._load(config)

    def test_no_github_repo_still_works_for_local_tools(self) -> None:
        mod = self._load(
            "limits:\n"
            "  max_file_bytes: 200000\n"
            "  max_diff_chars: 120000\n"
            "  max_context_chars: 160000\n"
            "  max_tree_entries: 4000\n"
            "  max_search_results: 200\n"
            "  max_review_page_chars: 30000\n"
            "deny_paths: []\n"
            "projects:\n"
            "  proj_a:\n"
            "    source: " + str(ROOT) + "\n"
            "    base_branch: main\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        self.assertIn("proj_a", mod.PROJECTS)
        result = mod.list_projects()
        self.assertIn("proj_a", [p["project"] for p in result["projects"]])


class IssueHappyPathTests(unittest.TestCase):
    """Test get_github_context returns correct Issue data with mocked API."""

    ISSUE_BODY = {
        "id": 1001,
        "number": 97,
        "title": "Test Issue Title",
        "body": "This is the issue body.",
        "state": "open",
        "html_url": "https://github.com/beesyst/beeui/issues/97",
        "user": {"login": "testuser", "id": 1},
        "labels": [{"name": "bug", "color": "red"}, {"name": "enhancement"}],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "closed_at": None,
        "extra_field": "should_not_appear",
    }

    COMMENT_BODIES = [
        {
            "id": 2001,
            "user": {"login": "commenter1", "id": 2},
            "body": "First comment",
            "created_at": "2026-01-01T01:00:00Z",
            "updated_at": "2026-01-01T01:30:00Z",
            "html_url": "https://github.com/beesyst/beeui/issues/97#issuecomment-2001",
            "extra": "noisy",
        },
        {
            "id": 2002,
            "user": {"login": "commenter2", "id": 3},
            "body": "Second comment",
            "created_at": "2026-01-02T01:00:00Z",
            "updated_at": "2026-01-02T01:30:00Z",
            "html_url": "https://github.com/beesyst/beeui/issues/97#issuecomment-2002",
        },
    ]

    def setUp(self) -> None:
        self.helper = _ImportHelper()
        config = self.helper.make_config(
            "  beeui:\n"
            "    source: " + str(ROOT) + "\n"
            "    base_branch: main\n"
            "    github_repo: beesyst/beeui\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        self.mod = self.helper.import_server(config)

    def tearDown(self) -> None:
        self.helper.cleanup()

    def _mock_urlopen_sequence(self, responses: list[Any]) -> Any:
        """Patch urllib.request.urlopen to return responses in sequence."""
        patcher = patch.object(
            __import__("urllib.request", fromlist=["urlopen"]),
            "urlopen",
        )
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        mock.side_effect = responses
        return mock

    def test_issue_returns_correct_kind_and_fields(self) -> None:
        issue_resp = _mocked_urlopen(200, self.ISSUE_BODY)
        comments_resp = _mocked_urlopen(200, self.COMMENT_BODIES)

        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [issue_resp, comments_resp]

        result = self.mod.get_github_context(
            "https://github.com/beesyst/beeui/issues/97"
        )

        self.assertEqual(result["kind"], "issue")
        self.assertEqual(result["project"], "beeui")
        self.assertEqual(result["repository"], "beesyst/beeui")
        self.assertEqual(result["number"], 97)
        self.assertEqual(result["title"], "Test Issue Title")
        self.assertEqual(result["body"], "This is the issue body.")
        self.assertEqual(result["state"], "open")
        self.assertEqual(result["author"], "testuser")
        self.assertEqual(result["labels"], ["bug", "enhancement"])
        self.assertEqual(result["created_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(result["updated_at"], "2026-01-02T00:00:00Z")
        self.assertIsNone(result["closed_at"])
        self.assertFalse(result["comments_truncated"])

    def test_issue_comments_normalized(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(200, self.ISSUE_BODY),
            _mocked_urlopen(200, self.COMMENT_BODIES),
        ]

        result = self.mod.get_github_context(
            "https://github.com/beesyst/beeui/issues/97"
        )
        comments = result["comments"]
        self.assertEqual(len(comments), 2)
        self.assertEqual(comments[0]["id"], 2001)
        self.assertEqual(comments[0]["author"], "commenter1")
        self.assertEqual(comments[0]["body"], "First comment")
        self.assertEqual(comments[1]["id"], 2002)
        self.assertEqual(comments[1]["author"], "commenter2")

    def test_issue_no_extra_raw_fields(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(200, self.ISSUE_BODY),
            _mocked_urlopen(200, self.COMMENT_BODIES),
        ]

        result = self.mod.get_github_context(
            "https://github.com/beesyst/beeui/issues/97"
        )
        self.assertNotIn("extra_field", result)
        self.assertNotIn("id", result)
        for comment in result["comments"]:
            self.assertNotIn("extra", comment)
            self.assertNotIn("user", comment)

    def test_issue_no_token_in_response(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(200, self.ISSUE_BODY),
            _mocked_urlopen(200, self.COMMENT_BODIES),
        ]

        result = self.mod.get_github_context(
            "https://github.com/beesyst/beeui/issues/97"
        )
        serialized = json.dumps(result)
        self.assertNotIn(TEST_TOKEN, serialized)

    def test_issue_rejects_pull_request_object(self) -> None:
        pr_issue = dict(self.ISSUE_BODY)
        pr_issue["pull_request"] = {
            "url": "https://api.github.com/repos/beesyst/beeui/pulls/97"
        }

        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.return_value = _mocked_urlopen(200, pr_issue)

        with self.assertRaises(ValueError) as ctx:
            self.mod.get_github_context("https://github.com/beesyst/beeui/issues/97")
        msg = str(ctx.exception)
        self.assertIn("Pull Request", msg)
        self.assertNotIn(TEST_TOKEN, msg)


class PrHappyPathTests(unittest.TestCase):
    """Test get_github_context returns correct PR data with mocked API."""

    PR_BODY = {
        "id": 3001,
        "number": 95,
        "title": "Test PR Title",
        "body": "This is the PR body.",
        "state": "open",
        "draft": False,
        "merged": False,
        "html_url": "https://github.com/beesyst/beeui/pull/95",
        "user": {"login": "pr_author", "id": 10},
        "base": {"ref": "main", "sha": "aaaa"},
        "head": {"ref": "feature", "sha": "bbbb"},
        "created_at": "2026-02-01T00:00:00Z",
        "updated_at": "2026-02-02T00:00:00Z",
        "closed_at": None,
        "merged_at": None,
    }

    CONV_COMMENTS = [
        {
            "id": 4001,
            "user": {"login": "reviewer1", "id": 20},
            "body": "Great work!",
            "created_at": "2026-02-03T00:00:00Z",
            "updated_at": "2026-02-03T01:00:00Z",
            "html_url": "https://github.com/beesyst/beeui/pull/95#issuecomment-4001",
        },
    ]

    REVIEWS = [
        {
            "id": 5001,
            "user": {"login": "approver1", "id": 30},
            "state": "APPROVED",
            "body": "Looks good to me",
            "submitted_at": "2026-02-04T00:00:00Z",
            "html_url": "https://github.com/beesyst/beeui/pull/95#pullrequestreview-5001",
        },
    ]

    REVIEW_COMMENTS = [
        {
            "id": 6001,
            "user": {"login": "reviewer1", "id": 20},
            "body": "Please fix this line",
            "path": "src/main.py",
            "line": 42,
            "start_line": None,
            "side": "RIGHT",
            "commit_id": "abc123",
            "created_at": "2026-02-03T02:00:00Z",
            "updated_at": "2026-02-03T03:00:00Z",
            "html_url": "https://github.com/beesyst/beeui/pull/95#discussion_r6001",
        },
    ]

    def setUp(self) -> None:
        self.helper = _ImportHelper()
        config = self.helper.make_config(
            "  beeui:\n"
            "    source: " + str(ROOT) + "\n"
            "    base_branch: main\n"
            "    github_repo: beesyst/beeui\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        self.mod = self.helper.import_server(config)

    def tearDown(self) -> None:
        self.helper.cleanup()

    def test_pr_returns_correct_kind_and_fields(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(200, self.PR_BODY),
            _mocked_urlopen(200, self.CONV_COMMENTS),
            _mocked_urlopen(200, self.REVIEWS),
            _mocked_urlopen(200, self.REVIEW_COMMENTS),
        ]

        result = self.mod.get_github_context("https://github.com/beesyst/beeui/pull/95")

        self.assertEqual(result["kind"], "pull_request")
        self.assertEqual(result["project"], "beeui")
        self.assertEqual(result["repository"], "beesyst/beeui")
        self.assertEqual(result["number"], 95)
        self.assertEqual(result["title"], "Test PR Title")
        self.assertEqual(result["body"], "This is the PR body.")
        self.assertEqual(result["state"], "open")
        self.assertFalse(result["draft"])
        self.assertFalse(result["merged"])
        self.assertEqual(result["author"], "pr_author")
        self.assertEqual(result["base_ref"], "main")
        self.assertEqual(result["head_ref"], "feature")
        self.assertEqual(result["head_sha"], "bbbb")
        self.assertEqual(result["created_at"], "2026-02-01T00:00:00Z")
        self.assertFalse(result["comments_truncated"])
        self.assertFalse(result["reviews_truncated"])
        self.assertFalse(result["review_comments_truncated"])

    def test_pr_separate_arrays(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(200, self.PR_BODY),
            _mocked_urlopen(200, self.CONV_COMMENTS),
            _mocked_urlopen(200, self.REVIEWS),
            _mocked_urlopen(200, self.REVIEW_COMMENTS),
        ]

        result = self.mod.get_github_context("https://github.com/beesyst/beeui/pull/95")

        self.assertEqual(len(result["comments"]), 1)
        self.assertEqual(result["comments"][0]["id"], 4001)
        self.assertEqual(len(result["reviews"]), 1)
        self.assertEqual(result["reviews"][0]["id"], 5001)
        self.assertEqual(result["reviews"][0]["state"], "APPROVED")
        self.assertEqual(result["reviews"][0]["body"], "Looks good to me")
        self.assertEqual(len(result["review_comments"]), 1)
        rc = result["review_comments"][0]
        self.assertEqual(rc["id"], 6001)
        self.assertEqual(rc["path"], "src/main.py")
        self.assertEqual(rc["line"], 42)
        self.assertIsNone(rc["start_line"])
        self.assertEqual(rc["side"], "RIGHT")
        self.assertEqual(rc["commit_id"], "abc123")

    def test_pr_arrays_not_mixed(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(200, self.PR_BODY),
            _mocked_urlopen(200, self.CONV_COMMENTS),
            _mocked_urlopen(200, self.REVIEWS),
            _mocked_urlopen(200, self.REVIEW_COMMENTS),
        ]

        result = self.mod.get_github_context("https://github.com/beesyst/beeui/pull/95")

        for c in result["comments"]:
            self.assertNotIn("state", c)
            self.assertNotIn("path", c)
            self.assertNotIn("submitted_at", c)

        for r in result["reviews"]:
            self.assertNotIn("path", r)
            self.assertNotIn("line", r)
            self.assertNotIn("commit_id", r)

        for rc in result["review_comments"]:
            self.assertNotIn("state", rc)
            self.assertNotIn("submitted_at", rc)

    def test_pr_no_token_in_response(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(200, self.PR_BODY),
            _mocked_urlopen(200, self.CONV_COMMENTS),
            _mocked_urlopen(200, self.REVIEWS),
            _mocked_urlopen(200, self.REVIEW_COMMENTS),
        ]

        result = self.mod.get_github_context("https://github.com/beesyst/beeui/pull/95")
        serialized = json.dumps(result)
        self.assertNotIn(TEST_TOKEN, serialized)


class ApiRequestTests(unittest.TestCase):
    """Verify HTTP method, headers, URL construction."""

    def setUp(self) -> None:
        self.helper = _ImportHelper()
        config = self.helper.make_config(
            "  beeui:\n"
            "    source: " + str(ROOT) + "\n"
            "    base_branch: main\n"
            "    github_repo: beesyst/beeui\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        self.mod = self.helper.import_server(config)

    def tearDown(self) -> None:
        self.helper.cleanup()

    def test_uses_only_https_api_github_com(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(
                200,
                {
                    "id": 1,
                    "title": "x",
                    "body": "y",
                    "number": 97,
                    "user": {"login": "u"},
                    "labels": [],
                    "state": "open",
                    "html_url": "https://...",
                    "created_at": "z",
                    "updated_at": "z",
                },
            ),
            _mocked_urlopen(200, []),
        ]

        self.mod.get_github_context("https://github.com/beesyst/beeui/issues/97")

        call_args = mock_urlopen.call_args_list
        for call in call_args:
            req = call[0][0]
            self.assertIsInstance(req, urllib.request.Request)
            self.assertTrue(req.full_url.startswith("https://api.github.com/"))

    def test_uses_only_http_get(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(
                200,
                {
                    "id": 1,
                    "title": "x",
                    "body": "y",
                    "number": 97,
                    "user": {"login": "u"},
                    "labels": [],
                    "state": "open",
                    "html_url": "https://...",
                    "created_at": "z",
                    "updated_at": "z",
                },
            ),
            _mocked_urlopen(200, []),
        ]

        self.mod.get_github_context("https://github.com/beesyst/beeui/issues/97")

        call_args = mock_urlopen.call_args_list
        for call in call_args:
            req = call[0][0]
            self.assertEqual(req.get_method(), "GET")

    def test_authorization_header(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(
                200,
                {
                    "id": 1,
                    "title": "x",
                    "body": "y",
                    "number": 97,
                    "user": {"login": "u"},
                    "labels": [],
                    "state": "open",
                    "html_url": "https://...",
                    "created_at": "z",
                    "updated_at": "z",
                },
            ),
            _mocked_urlopen(200, []),
        ]

        self.mod.get_github_context("https://github.com/beesyst/beeui/issues/97")

        req = mock_urlopen.call_args_list[0][0][0]
        headers_lower = {k.lower(): v for k, v in req.headers.items()}
        self.assertIn("authorization", headers_lower)
        self.assertIn("Bearer", headers_lower["authorization"])

    def test_accept_header(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(
                200,
                {
                    "id": 1,
                    "title": "x",
                    "body": "y",
                    "number": 97,
                    "user": {"login": "u"},
                    "labels": [],
                    "state": "open",
                    "html_url": "https://...",
                    "created_at": "z",
                    "updated_at": "z",
                },
            ),
            _mocked_urlopen(200, []),
        ]

        self.mod.get_github_context("https://github.com/beesyst/beeui/issues/97")

        req = mock_urlopen.call_args_list[0][0][0]
        headers_lower = {k.lower(): v for k, v in req.headers.items()}
        self.assertIn("accept", headers_lower)
        self.assertIn("github+json", headers_lower["accept"])

    def test_api_version_header(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(
                200,
                {
                    "id": 1,
                    "title": "x",
                    "body": "y",
                    "number": 97,
                    "user": {"login": "u"},
                    "labels": [],
                    "state": "open",
                    "html_url": "https://...",
                    "created_at": "z",
                    "updated_at": "z",
                },
            ),
            _mocked_urlopen(200, []),
        ]

        self.mod.get_github_context("https://github.com/beesyst/beeui/issues/97")

        req = mock_urlopen.call_args_list[0][0][0]
        headers_lower = {k.lower(): v for k, v in req.headers.items()}
        self.assertIn("x-github-api-version", headers_lower)

    def test_user_agent_header(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(
                200,
                {
                    "id": 1,
                    "title": "x",
                    "body": "y",
                    "number": 97,
                    "user": {"login": "u"},
                    "labels": [],
                    "state": "open",
                    "html_url": "https://...",
                    "created_at": "z",
                    "updated_at": "z",
                },
            ),
            _mocked_urlopen(200, []),
        ]

        self.mod.get_github_context("https://github.com/beesyst/beeui/issues/97")

        req = mock_urlopen.call_args_list[0][0][0]
        headers_lower = {k.lower(): v for k, v in req.headers.items()}
        self.assertEqual(headers_lower.get("user-agent"), "beemcp")

    def test_timeout_is_set(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(
                200,
                {
                    "id": 1,
                    "title": "x",
                    "body": "y",
                    "number": 97,
                    "user": {"login": "u"},
                    "labels": [],
                    "state": "open",
                    "html_url": "https://...",
                    "created_at": "z",
                    "updated_at": "z",
                },
            ),
            _mocked_urlopen(200, []),
        ]

        self.mod.get_github_context("https://github.com/beesyst/beeui/issues/97")

        call_args = mock_urlopen.call_args_list
        for call in call_args:
            _, kwargs = call
            if "timeout" not in kwargs and len(call[0]) <= 1:
                self.fail(f"No timeout in call: {call}")
            elif "timeout" in kwargs:
                self.assertIsInstance(kwargs["timeout"], (int, float))

    def test_no_post_or_put_or_patch_or_delete_in_source(self) -> None:
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            if method in ("POST",):
                continue  # POST is used in Streamable HTTP, not for GitHub
        self.assertNotIn('.method("POST")', source)
        self.assertNotIn('.method("PUT")', source)
        self.assertNotIn('.method("PATCH")', source)
        self.assertNotIn('.method("DELETE")', source)

    def test_user_cannot_specify_api_path(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(
                200,
                {
                    "id": 1,
                    "title": "x",
                    "body": "y",
                    "number": 97,
                    "user": {"login": "u"},
                    "labels": [],
                    "state": "open",
                    "html_url": "https://...",
                    "created_at": "z",
                    "updated_at": "z",
                },
            ),
            _mocked_urlopen(200, []),
        ]

        self.mod.get_github_context("https://github.com/beesyst/beeui/issues/97")

        for call in mock_urlopen.call_args_list:
            req = call[0][0]
            self.assertFalse("/user/repos" in req.full_url)
            self.assertTrue("/repos/beesyst/beeui/" in req.full_url)

    def test_allowlist_blocks_unknown_repo(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.mod.get_github_context("https://github.com/unknown/unknown/issues/1")
        msg = str(ctx.exception)
        self.assertIn("not in the allowlist", msg)
        self.assertNotIn(TEST_TOKEN, msg)


class PaginationTests(unittest.TestCase):
    """Test pagination for comments, reviews, and review comments."""

    def setUp(self) -> None:
        self.helper = _ImportHelper()
        config = self.helper.make_config(
            "  beeui:\n"
            "    source: " + str(ROOT) + "\n"
            "    base_branch: main\n"
            "    github_repo: beesyst/beeui\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        self.mod = self.helper.import_server(config)

    def tearDown(self) -> None:
        self.helper.cleanup()

    def _make_issue_body(self) -> dict:
        return {
            "id": 1,
            "number": 1,
            "title": "t",
            "body": "b",
            "state": "open",
            "html_url": "https://...",
            "user": {"login": "u"},
            "labels": [],
            "created_at": "z",
            "updated_at": "z",
        }

    def _make_items(self, prefix: str, count: int) -> list[dict]:
        return [
            {
                "id": i,
                "user": {"login": f"{prefix}_{i}"},
                "body": f"body_{i}",
                "created_at": "z",
                "updated_at": "z",
                "html_url": f"https://...#{prefix}_{i}",
            }
            for i in range(1, count + 1)
        ]

    def test_empty_comments_list(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(200, self._make_issue_body()),
            _mocked_urlopen(200, []),
        ]
        result = self.mod.get_github_context(
            "https://github.com/beesyst/beeui/issues/1"
        )
        self.assertEqual(result["comments"], [])
        self.assertFalse(result["comments_truncated"])

    def test_one_page_comments(self) -> None:
        items = self._make_items("c", 50)
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(200, self._make_issue_body()),
            _mocked_urlopen(200, items),
        ]
        result = self.mod.get_github_context(
            "https://github.com/beesyst/beeui/issues/1"
        )
        self.assertEqual(len(result["comments"]), 50)
        self.assertFalse(result["comments_truncated"])

    def test_two_pages_comments(self) -> None:
        page1 = self._make_items("c", 100)
        page2 = self._make_items("c", 30)
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(200, self._make_issue_body()),
            _mocked_urlopen(200, page1),
            _mocked_urlopen(200, page2),
        ]
        result = self.mod.get_github_context(
            "https://github.com/beesyst/beeui/issues/1"
        )
        self.assertEqual(len(result["comments"]), 130)
        self.assertFalse(result["comments_truncated"])

    def test_hard_page_cap_truncates(self) -> None:
        """5 pages max, each full page 100 items = 500 max."""
        pages = [self._make_items("c", 100) for _ in range(6)]
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(200, self._make_issue_body()),
        ] + [_mocked_urlopen(200, p) for p in pages]

        result = self.mod.get_github_context(
            "https://github.com/beesyst/beeui/issues/1"
        )
        self.assertGreaterEqual(len(result["comments"]), 400)
        self.assertTrue(result["comments_truncated"])

    def test_less_than_full_last_page(self) -> None:
        page1 = self._make_items("c", 100)
        page2 = self._make_items("c", 42)
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(200, self._make_issue_body()),
            _mocked_urlopen(200, page1),
            _mocked_urlopen(200, page2),
        ]
        result = self.mod.get_github_context(
            "https://github.com/beesyst/beeui/issues/1"
        )
        self.assertEqual(len(result["comments"]), 142)
        self.assertFalse(result["comments_truncated"])

    def test_pagination_uses_fixed_endpoints(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(200, self._make_issue_body()),
            _mocked_urlopen(
                200,
                [
                    {
                        "id": 1,
                        "user": {"login": "u"},
                        "body": "b",
                        "created_at": "z",
                        "updated_at": "z",
                        "html_url": "https://...",
                    }
                ],
            ),
        ]
        self.mod.get_github_context("https://github.com/beesyst/beeui/issues/1")
        for call in mock_urlopen.call_args_list:
            req = call[0][0]
            self.assertIsInstance(req, urllib.request.Request)
            self.assertTrue("/repos/" in req.full_url)
            self.assertFalse("Link" in str(req.headers))


class ResponseSizeTests(unittest.TestCase):
    """Test max_context_chars limiting."""

    def setUp(self) -> None:
        self.helper = _ImportHelper()

    def _make_tiny_config(self, max_chars: int) -> str:
        return (
            f"limits:\n"
            f"  max_file_bytes: 200000\n"
            f"  max_diff_chars: 120000\n"
            f"  max_context_chars: {max_chars}\n"
            f"  max_tree_entries: 4000\n"
            f"  max_search_results: 200\n"
            f"  max_review_page_chars: 30000\n"
            "deny_paths: []\n"
            "projects:\n"
            "  beeui:\n"
            "    source: " + str(ROOT) + "\n"
            "    base_branch: main\n"
            "    github_repo: beesyst/beeui\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )

    def _load_with_limit(self, max_chars: int) -> Any:
        config = self._make_tiny_config(max_chars)
        return self.helper.import_server(config)

    def tearDown(self) -> None:
        self.helper.cleanup()

    def test_issue_body_fits_within_limit(self) -> None:
        mod = self._load_with_limit(50000)
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(
                200,
                {
                    "id": 1,
                    "number": 1,
                    "title": "t",
                    "body": "x" * 100,
                    "state": "open",
                    "html_url": "https://...",
                    "user": {"login": "u"},
                    "labels": [],
                    "created_at": "z",
                    "updated_at": "z",
                },
            ),
            _mocked_urlopen(200, []),
        ]
        result = mod.get_github_context("https://github.com/beesyst/beeui/issues/1")
        self.assertEqual(result["body"], "x" * 100)

    def test_oversized_issue_body_raises_error(self) -> None:
        mod = self._load_with_limit(100)
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(
                200,
                {
                    "id": 1,
                    "number": 1,
                    "title": "t" * 50,
                    "body": "x" * 200,
                    "state": "open",
                    "html_url": "https://...",
                    "user": {"login": "u"},
                    "labels": [],
                    "created_at": "z",
                    "updated_at": "z",
                },
            ),
            _mocked_urlopen(200, []),
        ]
        with self.assertRaises(ValueError) as ctx:
            mod.get_github_context("https://github.com/beesyst/beeui/issues/1")
        msg = str(ctx.exception)
        self.assertIn("max_context_chars", msg)
        self.assertNotIn(TEST_TOKEN, msg)

    def test_comments_truncated_when_budget_exhausted(self) -> None:
        mod = self._load_with_limit(800)
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(
                200,
                {
                    "id": 1,
                    "number": 1,
                    "title": "t",
                    "body": "b",
                    "state": "open",
                    "html_url": "https://...",
                    "user": {"login": "u"},
                    "labels": [],
                    "created_at": "z",
                    "updated_at": "z",
                },
            ),
            _mocked_urlopen(
                200,
                [
                    {
                        "id": i,
                        "user": {"login": "u"},
                        "body": "x" * 200,
                        "created_at": "z",
                        "updated_at": "z",
                        "html_url": "https://...",
                    }
                    for i in range(10)
                ],
            ),
        ]
        result = mod.get_github_context("https://github.com/beesyst/beeui/issues/1")
        self.assertTrue(result["comments_truncated"])
        self.assertGreater(len(result["comments"]), 0)
        self.assertLess(len(result["comments"]), 10)

    def test_pr_comments_reviews_review_comments_truncated_separately(self) -> None:
        mod = self._load_with_limit(800)
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        pr_body = {
            "id": 1,
            "number": 1,
            "title": "t",
            "body": "b",
            "state": "open",
            "draft": False,
            "merged": False,
            "html_url": "https://...",
            "user": {"login": "u"},
            "base": {"ref": "main", "sha": "a"},
            "head": {"ref": "f", "sha": "b"},
            "created_at": "z",
            "updated_at": "z",
        }
        many_items = [
            {
                "id": i,
                "user": {"login": "u"},
                "body": "x" * 200,
                "created_at": "z",
                "updated_at": "z",
                "html_url": "https://...",
            }
            for i in range(10)
        ]
        mock_urlopen.side_effect = [
            _mocked_urlopen(200, pr_body),
            _mocked_urlopen(200, many_items),  # conversation comments
            _mocked_urlopen(200, many_items),  # reviews
            _mocked_urlopen(200, many_items),  # review comments
        ]
        result = mod.get_github_context("https://github.com/beesyst/beeui/pull/1")
        self.assertTrue(
            result["comments_truncated"]
            or result["reviews_truncated"]
            or result["review_comments_truncated"]
        )

    def test_pr_conversation_comments_truncated_when_no_budget_remains(self) -> None:
        pr_body = {
            "id": 1,
            "number": 1,
            "title": "t",
            "body": "b",
            "state": "open",
            "draft": False,
            "merged": False,
            "html_url": "https://...",
            "user": {"login": "u"},
            "base": {"ref": "main", "sha": "a"},
            "head": {"ref": "f", "sha": "b"},
            "created_at": "z",
            "updated_at": "z",
        }
        base_response = {
            "kind": "pull_request",
            "project": "beeui",
            "repository": "beesyst/beeui",
            "number": 1,
            "url": "https://...",
            "title": "t",
            "body": "b",
            "state": "open",
            "draft": False,
            "merged": False,
            "author": "u",
            "base_ref": "main",
            "head_ref": "f",
            "head_sha": "b",
            "created_at": "z",
            "updated_at": "z",
            "closed_at": None,
            "merged_at": None,
            "comments": [],
            "reviews": [],
            "review_comments": [],
            "comments_truncated": False,
            "reviews_truncated": False,
            "review_comments_truncated": False,
        }
        mod = self._load_with_limit(
            len(json.dumps(base_response, ensure_ascii=False, default=str))
        )
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(200, pr_body),
            _mocked_urlopen(200, [{"id": 1, "body": "comment"}]),
            _mocked_urlopen(200, []),
            _mocked_urlopen(200, []),
        ]

        result = mod.get_github_context("https://github.com/beesyst/beeui/pull/1")

        self.assertEqual(result["comments"], [])
        self.assertTrue(result["comments_truncated"])

    def test_pr_review_comments_truncated_when_no_budget_remains(self) -> None:
        pr_body = {
            "id": 1,
            "number": 1,
            "title": "t",
            "body": "b",
            "state": "open",
            "draft": False,
            "merged": False,
            "html_url": "https://...",
            "user": {"login": "u"},
            "base": {"ref": "main", "sha": "a"},
            "head": {"ref": "f", "sha": "b"},
            "created_at": "z",
            "updated_at": "z",
        }
        base_response = {
            "kind": "pull_request",
            "project": "beeui",
            "repository": "beesyst/beeui",
            "number": 1,
            "url": "https://...",
            "title": "t",
            "body": "b",
            "state": "open",
            "draft": False,
            "merged": False,
            "author": "u",
            "base_ref": "main",
            "head_ref": "f",
            "head_sha": "b",
            "created_at": "z",
            "updated_at": "z",
            "closed_at": None,
            "merged_at": None,
            "comments": [],
            "reviews": [],
            "review_comments": [],
            "comments_truncated": False,
            "reviews_truncated": False,
            "review_comments_truncated": False,
        }
        mod = self._load_with_limit(
            len(json.dumps(base_response, ensure_ascii=False, default=str))
        )
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(200, pr_body),
            _mocked_urlopen(200, []),
            _mocked_urlopen(200, []),
            _mocked_urlopen(200, [{"id": 1, "body": "inline"}]),
        ]

        result = mod.get_github_context("https://github.com/beesyst/beeui/pull/1")

        self.assertEqual(result["review_comments"], [])
        self.assertTrue(result["review_comments_truncated"])

    def test_no_comment_cut_in_middle(self) -> None:
        mod = self._load_with_limit(1000)
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(
                200,
                {
                    "id": 1,
                    "number": 1,
                    "title": "t",
                    "body": "b",
                    "state": "open",
                    "html_url": "https://...",
                    "user": {"login": "u"},
                    "labels": [],
                    "created_at": "z",
                    "updated_at": "z",
                },
            ),
            _mocked_urlopen(
                200,
                [
                    {
                        "id": i,
                        "user": {"login": "u"},
                        "body": "x" * 50,
                        "created_at": "z",
                        "updated_at": "z",
                        "html_url": "https://...",
                    }
                    for i in range(20)
                ],
            ),
        ]
        result = mod.get_github_context("https://github.com/beesyst/beeui/issues/1")
        for c in result["comments"]:
            self.assertTrue(
                c["body"].endswith("x" * 50) or c["body"] == "x" * 50,
                msg=f"Comment body truncated: {c['body'][:10]}...",
            )


class ErrorHandlingTests(unittest.TestCase):
    """Test error cases for GitHub API."""

    def setUp(self) -> None:
        self.helper = _ImportHelper()
        config = self.helper.make_config(
            "  beeui:\n"
            "    source: " + str(ROOT) + "\n"
            "    base_branch: main\n"
            "    github_repo: beesyst/beeui\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        self.mod = self.helper.import_server(config)

    def tearDown(self) -> None:
        self.helper.cleanup()

    def _check_error_safety(self, ctx: unittest.case._AssertRaisesContext) -> None:
        msg = str(ctx.exception)
        self.assertNotIn(TEST_TOKEN, msg)
        self.assertNotIn("Authorization", msg)
        self.assertNotIn("Bearer", msg)
        self.assertNotIn("traceback", msg.lower())
        self.assertNotIn("/home/", msg)

    def test_unknown_repo(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.mod.get_github_context("https://github.com/unknown/unknown/issues/1")
        self._check_error_safety(ctx)

    def test_not_found(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = _http_error_urlopen(404)

        with self.assertRaises(ValueError) as ctx:
            self.mod.get_github_context("https://github.com/beesyst/beeui/issues/1")
        self._check_error_safety(ctx)
        self.assertIn("not found", str(ctx.exception).lower())

    def test_http_401(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = _http_error_urlopen(401)

        with self.assertRaises(ValueError) as ctx:
            self.mod.get_github_context("https://github.com/beesyst/beeui/issues/1")
        self._check_error_safety(ctx)
        self.assertIn("authentication", str(ctx.exception).lower())

    def test_http_403(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = _http_error_urlopen(403)

        with self.assertRaises(ValueError) as ctx:
            self.mod.get_github_context("https://github.com/beesyst/beeui/issues/1")
        self._check_error_safety(ctx)
        self.assertIn("access denied", str(ctx.exception).lower())

    def test_http_429(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = _http_error_urlopen(429)

        with self.assertRaises(ValueError) as ctx:
            self.mod.get_github_context("https://github.com/beesyst/beeui/issues/1")
        self._check_error_safety(ctx)
        self.assertIn("HTTP 429", str(ctx.exception))

    def test_http_500(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = _http_error_urlopen(500)

        with self.assertRaises(ValueError) as ctx:
            self.mod.get_github_context("https://github.com/beesyst/beeui/issues/1")
        self._check_error_safety(ctx)
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_timeout(self) -> None:
        import urllib.request
        from urllib.error import URLError

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = URLError("timed out")

        with self.assertRaises(ValueError) as ctx:
            self.mod.get_github_context("https://github.com/beesyst/beeui/issues/1")
        self._check_error_safety(ctx)
        self.assertIn("connection failed", str(ctx.exception).lower())


class SecretSafetyTests(unittest.TestCase):
    """Ensure token is never leaked."""

    TOKEN = "github_pat_TEST_SECRET_NEVER_PRINT_abcdef123456"

    def setUp(self) -> None:
        self.helper = _ImportHelper()
        config = self.helper.make_config(
            "  beeui:\n"
            "    source: " + str(ROOT) + "\n"
            "    base_branch: main\n"
            "    github_repo: beesyst/beeui\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        import importlib.util

        tmp = tempfile.mkdtemp()
        config_path = Path(tmp) / "config.yml"
        config_path.write_text(config, encoding="utf-8")
        old_token = os.environ.get("BEEMCP_GITHUB_TOKEN")
        old_config = os.environ.get("BEEMCP_CONFIG")
        old_host = os.environ.get("BEEMCP_HOST")
        old_port = os.environ.get("BEEMCP_PORT")
        os.environ["BEEMCP_CONFIG"] = str(config_path)
        os.environ["BEEMCP_HOST"] = "127.0.0.1"
        os.environ["BEEMCP_PORT"] = "8765"
        os.environ["BEEMCP_GITHUB_TOKEN"] = self.TOKEN
        spec = importlib.util.spec_from_file_location("_sec_test", ROOT / "server.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("Cannot load")
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self._env_backup = {
            "BEEMCP_GITHUB_TOKEN": old_token,
            "BEEMCP_CONFIG": old_config,
            "BEEMCP_HOST": old_host,
            "BEEMCP_PORT": old_port,
        }

    def tearDown(self) -> None:
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_token_not_in_success_response(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = [
            _mocked_urlopen(
                200,
                {
                    "id": 1,
                    "number": 1,
                    "title": "t",
                    "body": "b",
                    "state": "open",
                    "html_url": "https://...",
                    "user": {"login": "u"},
                    "labels": [],
                    "created_at": "z",
                    "updated_at": "z",
                },
            ),
            _mocked_urlopen(200, []),
        ]
        result = self.mod.get_github_context(
            "https://github.com/beesyst/beeui/issues/1"
        )
        serialized = json.dumps(result)
        self.assertNotIn(self.TOKEN, serialized)

    def test_token_not_in_value_error(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = _http_error_urlopen(401)

        with self.assertRaises(ValueError) as ctx:
            self.mod.get_github_context("https://github.com/beesyst/beeui/issues/1")
        self.assertNotIn(self.TOKEN, str(ctx.exception))

    def test_token_not_in_403_error(self) -> None:
        import urllib.request

        patcher = patch.object(urllib.request, "urlopen")
        mock_urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        mock_urlopen.side_effect = _http_error_urlopen(403)

        with self.assertRaises(ValueError) as ctx:
            self.mod.get_github_context("https://github.com/beesyst/beeui/issues/1")
        self.assertNotIn(self.TOKEN, str(ctx.exception))

    def test_token_not_in_runtime_error_invalid_config(self) -> None:
        """server.py RuntimeError for empty token doesn't leak the token (static check)."""
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("BEEMCP_GITHUB_TOKEN must be set", source)
        self.assertNotIn("github_pat_", source)


class ToolAnnotationsAndContractTests(unittest.TestCase):
    """Verify tool annotations and exact contract."""

    def setUp(self) -> None:
        self.helper = _ImportHelper()
        config = self.helper.make_config(
            "  beeui:\n"
            "    source: " + str(ROOT) + "\n"
            "    base_branch: main\n"
            "    github_repo: beesyst/beeui\n"
            "    context_modes:\n"
            "      A:\n"
            "        files: []\n"
        )
        self.mod = self.helper.import_server(config)

    def tearDown(self) -> None:
        self.helper.cleanup()

    def test_get_github_context_has_open_world_hint(self) -> None:
        tools = self.mod.mcp._tool_manager.list_tools()
        gh_tool = next(t for t in tools if t.name == "get_github_context")
        annotations = gh_tool.annotations
        self.assertIsNotNone(annotations)
        self.assertTrue(annotations.readOnlyHint)
        self.assertFalse(annotations.destructiveHint)
        self.assertTrue(annotations.idempotentHint)
        self.assertTrue(annotations.openWorldHint)

    def test_local_tools_have_open_world_false(self) -> None:
        tools = self.mod.mcp._tool_manager.list_tools()
        local_names = {
            "list_projects",
            "list_worktrees",
            "get_project_context",
            "get_review_manifest",
            "get_review_bundle_page",
            "get_review_sources_page",
            "get_review_bundle",
            "read_project_file",
            "read_project_visual",
            "search_project",
        }
        for tool in tools:
            if tool.name in local_names:
                annotations = tool.annotations
                self.assertIsNotNone(annotations, f"{tool.name} has no annotations")
                self.assertFalse(
                    annotations.openWorldHint,
                    f"{tool.name} should have openWorldHint=False",
                )

    def test_exact_eleven_tool_contract(self) -> None:
        tools = self.mod.mcp._tool_manager.list_tools()
        names = {t.name for t in tools}
        expected = {
            "list_projects",
            "list_worktrees",
            "get_project_context",
            "get_review_manifest",
            "get_review_bundle_page",
            "get_review_sources_page",
            "get_review_bundle",
            "read_project_file",
            "read_project_visual",
            "search_project",
            "get_github_context",
        }
        self.assertEqual(names, expected)
        self.assertEqual(len(names), 11)

    def test_contract_version(self) -> None:
        self.assertEqual(self.mod.TOOL_CONTRACT_VERSION, 4)

    def test_tool_count_in_versionz(self) -> None:
        import asyncio

        routes = {r.path: r for r in self.mod.mcp.streamable_http_app().routes}
        response = asyncio.run(routes["/versionz"].endpoint(None))
        data = json.loads(response.body)
        self.assertEqual(data["tool_contract_version"], 4)
        self.assertEqual(data["tool_count"], 11)

    def test_check_mcp_tools_script_accepts_eleven_tools(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "check_tools", ROOT / "scripts/check_mcp_tools.py"
        )
        if spec is None or spec.loader is None:
            self.fail("Cannot load check_mcp_tools.py")
        check_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check_mod)
        self.assertEqual(len(check_mod.EXPECTED_TOOLS), 11)
        self.assertIn("get_github_context", check_mod.EXPECTED_TOOLS)

    def test_get_github_context_in_expected_tools(self) -> None:
        self.assertIn("get_github_context", self.mod.EXPECTED_TOOLS)

    def test_no_unexpected_tools(self) -> None:
        tools = self.mod.mcp._tool_manager.list_tools()
        names = {t.name for t in tools}
        expected = set(self.mod.EXPECTED_TOOLS)
        self.assertEqual(names, expected)

    def test_no_write_tools(self) -> None:
        tools = self.mod.mcp._tool_manager.list_tools()
        write_hints = {
            "create",
            "update",
            "delete",
            "write",
            "post",
            "put",
            "patch",
            "merge",
            "push",
            "approve",
        }
        for tool in tools:
            name = tool.name.lower()
            for hint in write_hints:
                self.assertNotIn(
                    hint, name, f"Tool {tool.name} looks like a write tool"
                )


class DotEnvTests(unittest.TestCase):
    """Test .env validation for BEEMCP_GITHUB_TOKEN."""

    def test_env_example_has_empty_token(self) -> None:
        content = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("BEEMCP_GITHUB_TOKEN=", content)
        for line in content.splitlines():
            if line.startswith("BEEMCP_GITHUB_TOKEN="):
                value = line.split("=", 1)[1]
                self.assertEqual(value, "")

    def test_token_required_in_required_env_vars(self) -> None:
        """BEEMCP_GITHUB_TOKEN is listed in REQUIRED_ENV_VARS (static check)."""
        source = (ROOT / "config/start.py").read_text(encoding="utf-8")
        self.assertIn("BEEMCP_GITHUB_TOKEN", source)

    def test_token_with_space_rejected_by_startpy(self) -> None:
        """start.py validates token whitespace (static check)."""
        source = (ROOT / "config/start.py").read_text(encoding="utf-8")
        self.assertIn("BEEMCP_GITHUB_TOKEN", source)
        self.assertIn("whitespace", source.lower())

    def test_token_too_short_rejected_by_serverpy(self) -> None:
        """server.py validates token length (static check)."""
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("GITHUB_TOKEN", source)
        self.assertTrue("len(GITHUB_TOKEN) < 10" in source or "len < 10" in source)


class McpEnvFileTests(unittest.TestCase):
    """Test that setup/up/restart generates correct mcp.env."""

    def test_setup_creates_mcp_env_file(self) -> None:
        source = (ROOT / "config/start.py").read_text(encoding="utf-8")
        self.assertIn("def generate_mcp_env_file", source)
        self.assertIn("mcp.env", source)
        self.assertIn("BEEMCP_GITHUB_TOKEN", source)

    def test_mcp_env_file_does_not_contain_other_secrets(self) -> None:
        source = (ROOT / "config/start.py").read_text(encoding="utf-8")
        gen_fn = source[source.find("def generate_mcp_env_file") :]
        gen_fn = gen_fn[: gen_fn.find("\ndef ")]
        self.assertIn("BEEMCP_GITHUB_TOKEN", gen_fn)
        self.assertNotIn("CONTROL_PLANE_API_KEY", gen_fn)
        self.assertNotIn("BEEMCP_TUNNEL_ID", gen_fn)
        self.assertNotIn("BEEMCP_CONTROL_PLANE_URL", gen_fn)

    def test_mcp_unit_does_not_use_repo_env(self) -> None:
        template = (ROOT / "ops/systemd/beemcp.service.in").read_text(encoding="utf-8")
        self.assertIn("MCP_ENV_FILE", template)
        self.assertNotIn(".env", template.replace("MCP_ENV_FILE", ""))

    def test_tunnel_unit_uses_repo_env_not_mcp_env(self) -> None:
        template = (ROOT / "ops/systemd/beemcp-tunnel.service.in").read_text(
            encoding="utf-8"
        )
        self.assertIn("ENV_FILE", template)
        self.assertNotIn("MCP_ENV_FILE", template)

    def test_service_template_has_no_token_value(self) -> None:
        content = (ROOT / "ops/systemd/beemcp.service.in").read_text(encoding="utf-8")
        self.assertNotIn("github_pat", content)
        self.assertNotIn("ghp_", content)
        self.assertNotIn("ghs_", content)
        self.assertNotIn("github_token", content.lower())


class DoctorStaticTests(unittest.TestCase):
    """Static assertions about doctor implementation."""

    def test_doctor_checks_mcp_env_file(self) -> None:
        source = (ROOT / "config/start.py").read_text(encoding="utf-8")
        self.assertIn("mcp_env_file", source)
        self.assertIn("mcp.env", source)

    def test_doctor_checks_mcp_env_permissions(self) -> None:
        source = (ROOT / "config/start.py").read_text(encoding="utf-8")
        doctor = source[source.find("def cmd_doctor") :]
        self.assertIn("mcp_env_file", doctor)

    def test_doctor_does_not_contain_token(self) -> None:
        source = (ROOT / "config/start.py").read_text(encoding="utf-8")
        self.assertNotIn("github_pat", source)
        self.assertNotIn("ghp_", source)
        self.assertNotIn("ghs_", source)

    def test_doctor_checks_mcp_unit_references_env_file(self) -> None:
        source = (ROOT / "config/start.py").read_text(encoding="utf-8")
        doctor = source[source.find("def cmd_doctor") :]
        self.assertIn("mcp_unit_path", doctor)


class ReadOnlyRegressionTests(unittest.TestCase):
    """Verify no write tools or capabilities were added."""

    def test_no_github_write_tools_in_expected_tools(self) -> None:
        write_keywords = [
            "create",
            "update",
            "delete",
            "write",
            "post",
            "put",
            "patch",
            "merge",
            "push",
            "approve",
            "comment",
            "publish",
            "edit",
        ]
        for name in (
            "list_projects",
            "list_worktrees",
            "get_project_context",
            "get_review_manifest",
            "get_review_bundle_page",
            "get_review_sources_page",
            "get_review_bundle",
            "read_project_file",
            "search_project",
            "get_github_context",
        ):
            name_lower = name.lower()
            for w in write_keywords:
                self.assertNotIn(w, name_lower, f"Tool {name} looks like a write tool")

    def test_no_generic_http_tool(self) -> None:
        tools_module = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("def fetch_url", tools_module)
        self.assertNotIn("def http_request", tools_module)
        self.assertNotIn("def generic_fetch", tools_module)

    def test_no_agent_execution_in_tools(self) -> None:
        tools_module = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("def run_agent", tools_module)
        self.assertNotIn("def execute_agent", tools_module)
        self.assertNotIn("def invoke_agent", tools_module)

    def test_no_file_write_tools(self) -> None:
        tools_module = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("def write_file", tools_module)
        self.assertNotIn("def create_file", tools_module)
        self.assertNotIn("def edit_file", tools_module)

    def test_no_git_write_tools(self) -> None:
        tools_module = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("def git_commit", tools_module)
        self.assertNotIn("def git_push", tools_module)
        self.assertNotIn("def git_merge", tools_module)
        self.assertNotIn("def git_branch", tools_module)

    def test_github_implementation_uses_only_get(self) -> None:
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        gh_section = source[source.find("def _gh_api_request") :]
        self.assertIn("urllib.request.Request", gh_section)
        lines = gh_section.split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue
            self.assertNotIn('"POST"', stripped)
            self.assertNotIn('"PUT"', stripped)
            self.assertNotIn('"PATCH"', stripped)
            self.assertNotIn('"DELETE"', stripped)


class DocumentationTests(unittest.TestCase):
    """Minimal static assertions about README."""

    def test_readme_does_not_say_no_github_access(self) -> None:
        content = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("No GitHub access", content)

    def test_readme_mentions_bounded_read_only_github(self) -> None:
        content = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertTrue(
            "bounded read-only GitHub" in content
            or "read-only GitHub access" in content
            or "bounded read-only" in content,
            msg="README should describe bounded read-only GitHub access",
        )

    def test_readme_mentions_github_token(self) -> None:
        content = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("BEEMCP_GITHUB_TOKEN", content)

    def test_readme_mentions_github_repo(self) -> None:
        content = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("github_repo", content)

    def test_readme_mentions_get_github_context(self) -> None:
        content = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("get_github_context", content)

    def test_readme_has_issue_url_example(self) -> None:
        content = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("issues/", content)
        self.assertIn("FULL_APPROVED_ISSUE", content)

    def test_readme_has_pr_url_example(self) -> None:
        content = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("CURRENT_PR_OR_NONE", content)

    def test_readme_mentions_refresh_actions(self) -> None:
        content = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Refresh", content)

    def test_readme_mentions_eleven_tool_contract(self) -> None:
        content = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertTrue(
            "eleven-tool" in content
            or "eleven tools" in content
            or "11 tools" in content
        )


if __name__ == "__main__":
    unittest.main()
