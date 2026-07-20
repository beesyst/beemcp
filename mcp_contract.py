from __future__ import annotations

import hashlib
from collections.abc import Iterable


EXPECTED_TOOLS = (
    "list_projects",
    "list_worktrees",
    "get_project_context",
    "get_review_manifest",
    "get_review_bundle_page",
    "get_review_bundle",
    "read_project_file",
    "search_project",
)

TOOL_CONTRACT_VERSION = 2


def tool_contract_fingerprint(tool_names: Iterable[str]) -> str:
    payload = "\n".join(sorted(tool_names)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()