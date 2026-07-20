"""Architecture boundary tests."""

import ast
from pathlib import Path


def test_core_does_not_import_provider_sdks() -> None:
    forbidden_roots = {"anthropic", "github", "gitlab", "openai"}
    source_roots = [
        Path("src/revio/domain"),
        Path("src/revio/application"),
        Path("src/revio/ports"),
    ]
    imports: set[str] = set()
    for path in (path for root in source_roots for path in root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
    assert imports.isdisjoint(forbidden_roots)


def test_core_does_not_import_github_adapter_modules() -> None:
    violations: list[tuple[Path, str]] = []
    for root in (Path("src/revio/domain"), Path("src/revio/application"), Path("src/revio/ports")):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                else:
                    names = []
                violations.extend(
                    (path, name)
                    for name in names
                    if name == "revio.adapters.scm.github"
                    or name.startswith("revio.adapters.scm.github.")
                )
    assert violations == []


def test_github_dtos_are_isolated_to_adapter() -> None:
    github_imports: list[Path] = []
    for path in Path("src/revio").rglob("*.py"):
        if "adapters/scm/github" in path.as_posix():
            continue
        if "revio.adapters.scm.github.dto" in path.read_text():
            github_imports.append(path)
    assert github_imports == []
