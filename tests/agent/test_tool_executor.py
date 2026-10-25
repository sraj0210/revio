"""Security matrix for the guarded agent tool executor.

The executor is the single place the core enforces what the model may read. These tests cover path
traversal, the secret deny-list, not-found, oversize files, and the per-file/aggregate/tool-call
budgets, plus provenance and tree completeness.
"""

import json

import pytest

from revio.application.review.agent_tools import AgentLimits, AgentToolExecutor
from revio.domain.agent import AgentToolRequest
from revio.domain.models import (
    ChangeRequestTarget,
    CollectionCompleteness,
    RepositoryEntry,
    TreeCollection,
)


class FakeContent:
    def __init__(
        self,
        files: dict[str, str] | None = None,
        tree: TreeCollection | None = None,
    ) -> None:
        self._files = files or {}
        self._tree = tree or TreeCollection()
        self.file_calls: list[str] = []

    async def get_file(self, target: ChangeRequestTarget, path: str, ref: str) -> str | None:
        self.file_calls.append(path)
        return self._files.get(path)

    async def get_tree(self, target: ChangeRequestTarget, path: str, ref: str) -> TreeCollection:
        return self._tree


def _req(name: str, path: object, call_id: str = "c1") -> AgentToolRequest:
    return AgentToolRequest(call_id=call_id, name=name, arguments={"path": path})


def _executor(content: FakeContent, target: ChangeRequestTarget, **kw: int) -> AgentToolExecutor:
    return AgentToolExecutor(content, target, "headsha", AgentLimits(**kw))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "../secrets.txt",
        "../../etc/passwd",
        "/etc/passwd",
        "a/../../b",
        "C:\\win",
        "bad\\slash",
        "with\x00null",
        "ctl\tchar",
        "",
    ],
)
async def test_unsafe_paths_are_refused(target: ChangeRequestTarget, path: str) -> None:
    content = FakeContent(files={"ok.py": "x"})
    executor = _executor(content, target)
    (result,) = await executor.execute((_req("get_file", path),))
    assert result.ok is False
    assert content.file_calls == []  # never reached the content port


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [".env", "config/.env.local", "deploy/app.pem", "keys/id_rsa", "secrets/token", ".git/config"],
)
async def test_secret_denylist_is_never_returned(target: ChangeRequestTarget, path: str) -> None:
    content = FakeContent(files={path: "SENSITIVE"})
    executor = _executor(content, target)
    (result,) = await executor.execute((_req("get_file", path),))
    assert result.ok is False
    assert "SENSITIVE" not in result.content
    assert content.file_calls == []


@pytest.mark.asyncio
async def test_unknown_tool_is_refused(target: ChangeRequestTarget) -> None:
    executor = _executor(FakeContent(), target)
    (result,) = await executor.execute(
        (AgentToolRequest(call_id="c1", name="run_shell", arguments={"path": "a"}),)
    )
    assert result.ok is False
    assert result.content == "unknown tool"


@pytest.mark.asyncio
async def test_missing_file_is_a_definitive_not_found(target: ChangeRequestTarget) -> None:
    executor = _executor(FakeContent(files={}), target)
    (result,) = await executor.execute((_req("get_file", "nope.py"),))
    assert result.ok is False
    assert result.content == "file was not found"
    # Not-found is a definitive answer; context stays complete.
    assert executor.provenance().complete is True


@pytest.mark.asyncio
async def test_oversize_file_is_refused_and_marks_incomplete(target: ChangeRequestTarget) -> None:
    content = FakeContent(files={"big.py": "x" * 100})
    executor = _executor(content, target, max_bytes_per_file=10)
    (result,) = await executor.execute((_req("get_file", "big.py"),))
    assert result.ok is False
    assert "x" * 100 not in result.content
    assert executor.provenance().complete is False


@pytest.mark.asyncio
async def test_file_count_budget_exhaustion(target: ChangeRequestTarget) -> None:
    content = FakeContent(files={"a.py": "a", "b.py": "b"})
    executor = _executor(content, target, max_files=1)
    first = await executor.execute((_req("get_file", "a.py"),))
    second = await executor.execute((_req("get_file", "b.py", call_id="c2"),))
    assert first[0].ok is True
    assert second[0].ok is False
    assert executor.provenance().complete is False


@pytest.mark.asyncio
async def test_aggregate_byte_budget_exhaustion(target: ChangeRequestTarget) -> None:
    content = FakeContent(files={"a.py": "x" * 8, "b.py": "y" * 8})
    executor = _executor(content, target, max_aggregate_bytes=10)
    first = await executor.execute((_req("get_file", "a.py"),))
    second = await executor.execute((_req("get_file", "b.py", call_id="c2"),))
    assert first[0].ok is True
    assert second[0].ok is False
    assert executor.provenance().complete is False


@pytest.mark.asyncio
async def test_tool_call_budget_exhaustion(target: ChangeRequestTarget) -> None:
    content = FakeContent(files={"a.py": "a"})
    executor = _executor(content, target, max_tool_calls=1)
    first = await executor.execute((_req("get_file", "a.py"),))
    assert first[0].ok is True
    assert executor.exhausted is True
    second = await executor.execute((_req("get_file", "a.py", call_id="c2"),))
    assert second[0].ok is False
    assert "budget" in second[0].content


@pytest.mark.asyncio
async def test_successful_file_records_provenance(target: ChangeRequestTarget) -> None:
    content = FakeContent(files={"src/a.py": "def f(): pass"})
    executor = _executor(content, target)
    (result,) = await executor.execute((_req("get_file", "src/a.py"),))
    assert result.ok is True
    assert result.content == "def f(): pass"
    prov = executor.provenance()
    assert prov.complete is True
    assert prov.consulted[0].path == "src/a.py"
    assert prov.consulted[0].ref == "headsha"
    assert prov.consulted[0].kind == "get_file"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["config.PEM", "deploy/App.Key", "ID_RSA", ".ENV", "Secrets/token", "config/.Git/x"],
)
async def test_secret_denylist_is_case_insensitive(target: ChangeRequestTarget, path: str) -> None:
    content = FakeContent(files={path: "SENSITIVE"})
    executor = _executor(content, target)
    (result,) = await executor.execute((_req("get_file", path),))
    assert result.ok is False
    assert "SENSITIVE" not in result.content
    assert content.file_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["prod.env", "infra/main.tfstate", "backup/app.tfvars", "k.gpg"])
async def test_broadened_secret_globs(target: ChangeRequestTarget, path: str) -> None:
    content = FakeContent(files={path: "SENSITIVE"})
    executor = _executor(content, target)
    (result,) = await executor.execute((_req("get_file", path),))
    assert result.ok is False
    assert content.file_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["a\x7fb.py", "c\x85d.py", "e\x9ff.py"])
async def test_del_and_c1_control_chars_are_refused(target: ChangeRequestTarget, path: str) -> None:
    content = FakeContent(files={path: "x"})
    executor = _executor(content, target)
    (result,) = await executor.execute((_req("get_file", path),))
    assert result.ok is False
    assert content.file_calls == []


@pytest.mark.asyncio
async def test_content_port_error_degrades_to_refusal(target: ChangeRequestTarget) -> None:
    class Raising:
        async def get_file(self, t: ChangeRequestTarget, path: str, ref: str) -> str | None:
            raise RuntimeError("upstream exploded")

        async def get_tree(self, t: ChangeRequestTarget, path: str, ref: str) -> TreeCollection:
            raise RuntimeError("upstream exploded")

    executor = AgentToolExecutor(Raising(), target, "headsha", AgentLimits())  # type: ignore[arg-type]
    (file_result,) = await executor.execute((_req("get_file", "a.py"),))
    (tree_result,) = await executor.execute((_req("get_tree", ""),))
    assert file_result.ok is False and "unavailable" in file_result.content
    assert tree_result.ok is False and "unavailable" in tree_result.content
    assert executor.provenance().complete is False


@pytest.mark.asyncio
async def test_get_tree_omits_excluded_paths_and_flags_incomplete(
    target: ChangeRequestTarget,
) -> None:
    tree = TreeCollection(
        items=(
            RepositoryEntry(path="src/a.py", entry_type="blob", sha="s1"),
            RepositoryEntry(path=".env", entry_type="blob", sha="s2"),
            RepositoryEntry(path="keys/id_rsa", entry_type="blob", sha="s3"),
        )
    )
    executor = _executor(FakeContent(tree=tree), target)
    (result,) = await executor.execute((_req("get_tree", ""),))
    assert result.ok is True
    paths = {entry["path"] for entry in json.loads(result.content)["entries"]}
    assert paths == {"src/a.py"}  # secret paths are never enumerated to the model
    assert executor.provenance().complete is False


@pytest.mark.asyncio
async def test_get_tree_counts_bytes_toward_aggregate_budget(
    target: ChangeRequestTarget,
) -> None:
    tree = TreeCollection(
        items=tuple(
            RepositoryEntry(path=f"dir/file_{i}.py", entry_type="blob", sha="s") for i in range(50)
        )
    )
    executor = _executor(FakeContent(tree=tree), target, max_aggregate_bytes=50)
    (result,) = await executor.execute((_req("get_tree", ""),))
    assert result.ok is False
    assert "budget" in result.content
    assert executor.provenance().complete is False


@pytest.mark.asyncio
async def test_tree_lists_entries_and_flags_incompleteness(target: ChangeRequestTarget) -> None:
    tree = TreeCollection(
        items=(
            RepositoryEntry(path="a.py", entry_type="blob", sha="s1"),
            RepositoryEntry(path="pkg", entry_type="tree", sha="s2"),
        ),
        completeness=CollectionCompleteness(status="service_item_limit"),
    )
    executor = _executor(FakeContent(tree=tree), target)
    (result,) = await executor.execute((_req("get_tree", ""),))
    assert result.ok is True
    body = json.loads(result.content)
    assert {"path": "a.py", "type": "blob"} in body["entries"]
    # A non-complete tree marks the overall context incomplete.
    assert executor.provenance().complete is False
