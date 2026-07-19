"""Architecture boundary tests."""

import ast
from pathlib import Path


def test_core_does_not_import_provider_sdks() -> None:
    forbidden_roots = {"anthropic", "github", "gitlab", "openai"}
    source_root = Path("src/revio")
    imports: set[str] = set()
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
    assert imports.isdisjoint(forbidden_roots)
