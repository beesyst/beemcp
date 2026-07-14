#!/usr/bin/env bash
set -euo pipefail

repo="${HOME}/Pro/mcp-demo"
mkdir -p "$repo"
cd "$repo"

if [[ ! -d .git ]]; then
  git init -b main
fi

cat > calculator.py <<'PY'
def add(a: float, b: float) -> float:
    return a + b
PY

cat > test_calculator.py <<'PY'
import unittest

from calculator import add


class CalculatorTests(unittest.TestCase):
    def test_add(self) -> None:
        self.assertEqual(add(2, 3), 5)


if __name__ == "__main__":
    unittest.main()
PY

cat > README.md <<'MD'
# MCP demo

Small disposable repository for testing the ChatGPT → MCP → Copilot/Codex workflow.
MD

git add calculator.py test_calculator.py README.md
if ! git diff --cached --quiet; then
  git commit -m "chore: initialize MCP demo"
fi

echo "Created: $repo"
