from __future__ import annotations

import hashlib
from collections.abc import Iterable

EXPECTED_TOOLS = (
    "list_projects",
    "list_worktrees",
    "get_project_context",
    "get_review_manifest",
    "get_review_bundle_page",
    "get_review_sources_page",
    "get_review_bundle",
    "read_project_file",
    "read_local_artifact",
    "read_project_visual",
    "search_project",
    "get_github_context",
)

TOOL_CONTRACT_VERSION = 5


def tool_contract_fingerprint(tool_names: Iterable[str]) -> str:
    payload = "\n".join(sorted(tool_names)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
