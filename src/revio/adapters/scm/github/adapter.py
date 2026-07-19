"""Read-only GitHub implementation of SCM read ports."""

import base64
from typing import Literal, cast
from urllib.parse import quote

from pydantic import ValidationError
from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from revio.adapters.scm.github.client import GitHubClient
from revio.adapters.scm.github.dto.api import (
    GitHubContentDTO,
    GitHubFileDTO,
    GitHubPullRequestDTO,
    GitHubTreeDTO,
)
from revio.adapters.scm.github.errors import (
    GitHubNotFoundError,
    GitHubResponseError,
    GitHubUnsupportedObjectError,
)
from revio.domain.identifiers import ChangeRequestTarget
from revio.domain.models import ChangeRequest, DiffFile, DiffLine, RepositoryEntry


class GitHubReadAdapter:
    def __init__(
        self,
        client: GitHubClient,
        *,
        max_pages: int = 30,
        max_items: int = 3_000,
        max_file_bytes: int = 1_000_000,
    ) -> None:
        self._client, self._max_pages, self._max_items = client, max_pages, max_items
        self._max_file_bytes = max_file_bytes

    @staticmethod
    def _coordinates(target: ChangeRequestTarget) -> tuple[int, str, str]:
        repository = target.repository
        if repository.owner is None or repository.name is None:
            raise GitHubResponseError("repository coordinates are required")
        try:
            installation_id = int(repository.installation.external_id)
        except ValueError as error:
            raise GitHubResponseError("invalid installation identity") from error
        return installation_id, repository.owner, repository.name

    @staticmethod
    def _path(path: str) -> list[str]:
        if path.startswith("/"):
            raise GitHubResponseError("repository path must be relative")
        parts = path.split("/") if path else []
        if any(part in {"", ".", ".."} for part in parts):
            raise GitHubResponseError("invalid repository path")
        return parts

    async def get_change_request(self, target: ChangeRequestTarget) -> ChangeRequest:
        installation, owner, repo = self._coordinates(target)
        response = await self._client.get(
            installation, f"/repos/{quote(owner)}/{quote(repo)}/pulls/{target.external_number}"
        )
        try:
            dto = GitHubPullRequestDTO.model_validate(response.json())
        except (ValidationError, ValueError) as error:
            raise GitHubResponseError("invalid GitHub pull request response") from error
        state = "merged" if dto.merged else dto.state
        return ChangeRequest(
            target=target,
            title=dto.title,
            description=dto.body or "",
            base_sha=dto.base.sha,
            head_sha=dto.head.sha,
            state=state,
            draft=dto.draft,
        )

    async def get_diff(self, target: ChangeRequestTarget) -> list[DiffFile]:
        installation, owner, repo = self._coordinates(target)
        items: list[GitHubFileDTO] = []
        for page in range(1, self._max_pages + 1):
            response = await self._client.get(
                installation,
                f"/repos/{quote(owner)}/{quote(repo)}/pulls/{target.external_number}/files",
                params={"per_page": 100, "page": page},
            )
            try:
                batch = [GitHubFileDTO.model_validate(item) for item in response.json()]
            except (ValidationError, ValueError, TypeError) as error:
                raise GitHubResponseError("invalid GitHub changed-files response") from error
            items.extend(batch)
            if len(items) > self._max_items:
                raise GitHubResponseError("GitHub changed-files result exceeds configured limit")
            if len(batch) < 100:
                break
        else:
            raise GitHubResponseError("GitHub changed-files pagination limit reached")
        return [self._map_file(item) for item in items]

    @staticmethod
    def _map_file(item: GitHubFileDTO) -> DiffFile:
        statuses = {
            "removed": "deleted",
            "copied": "added",
            "changed": "modified",
            "unchanged": "modified",
        }
        status = cast(
            Literal["added", "modified", "deleted", "renamed"],
            statuses.get(item.status, item.status),
        )
        lines: list[DiffLine] = []
        truncated = item.patch is None
        if item.patch is not None:
            try:
                header = f"--- a/{item.previous_filename or item.filename}\n+++ b/{item.filename}\n"
                patch = PatchSet(header + item.patch)
                for patched_file in patch:
                    for hunk in patched_file:
                        for line in hunk:
                            if line.is_added:
                                lines.append(
                                    DiffLine(
                                        content=line.value.rstrip("\n"),
                                        side="new",
                                        new_line=line.target_line_no,
                                    )
                                )
                            elif line.is_removed:
                                lines.append(
                                    DiffLine(
                                        content=line.value.rstrip("\n"),
                                        side="old",
                                        old_line=line.source_line_no,
                                    )
                                )
            except UnidiffParseError:
                truncated = True
                lines = []
        return DiffFile(
            old_path=item.previous_filename,
            new_path=item.filename,
            status=status,
            lines=tuple(lines),
            truncated=truncated,
        )

    async def get_file(self, target: ChangeRequestTarget, path: str, ref: str) -> str | None:
        installation, owner, repo = self._coordinates(target)
        parts = self._path(path)
        if not parts:
            raise GitHubResponseError("file path is required")
        encoded_path = "/".join(quote(part, safe="") for part in parts)
        try:
            response = await self._client.get(
                installation,
                f"/repos/{quote(owner)}/{quote(repo)}/contents/{encoded_path}",
                params={"ref": ref},
            )
        except GitHubNotFoundError:
            return None
        try:
            dto = GitHubContentDTO.model_validate(response.json())
        except (ValidationError, ValueError) as error:
            raise GitHubResponseError("invalid GitHub content response") from error
        if dto.type != "file":
            raise GitHubUnsupportedObjectError("GitHub object is not a file")
        if dto.encoding != "base64" or dto.content is None:
            raise GitHubUnsupportedObjectError("unsupported GitHub file encoding")
        try:
            raw = base64.b64decode("".join(dto.content.split()), validate=True)
        except ValueError as error:
            raise GitHubResponseError("invalid GitHub file content") from error
        if len(raw) > self._max_file_bytes:
            raise GitHubResponseError("GitHub file exceeds configured limit")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise GitHubUnsupportedObjectError("GitHub file is not UTF-8 text") from error

    async def get_tree(
        self, target: ChangeRequestTarget, path: str, ref: str
    ) -> list[RepositoryEntry]:
        installation, owner, repo = self._coordinates(target)
        commit = await self._client.get(
            installation, f"/repos/{quote(owner)}/{quote(repo)}/commits/{quote(ref, safe='')}"
        )
        try:
            tree_sha = commit.json()["commit"]["tree"]["sha"]
            if not isinstance(tree_sha, str):
                raise TypeError
        except (KeyError, TypeError, ValueError) as error:
            raise GitHubResponseError("invalid GitHub commit response") from error
        segments = self._path(path)
        prefix_parts: list[str] = []
        dto: GitHubTreeDTO | None = None
        for request_number in range(len(segments) + 1):
            if request_number >= self._max_pages:
                raise GitHubResponseError("GitHub tree request limit reached")
            response = await self._client.get(
                installation,
                f"/repos/{quote(owner)}/{quote(repo)}/git/trees/{quote(tree_sha, safe='')}",
            )
            try:
                dto = GitHubTreeDTO.model_validate(response.json())
            except (ValidationError, ValueError) as error:
                raise GitHubResponseError("invalid GitHub tree response") from error
            if dto.truncated or len(dto.tree) > self._max_items:
                raise GitHubResponseError("GitHub tree is truncated or exceeds configured limit")
            if request_number == len(segments):
                break
            segment = segments[request_number]
            child = next((entry for entry in dto.tree if entry.path == segment), None)
            if child is None:
                return []
            if child.type != "tree":
                raise GitHubUnsupportedObjectError("GitHub tree path is not a directory")
            tree_sha = child.sha
            prefix_parts.append(segment)
        assert dto is not None
        prefix = "/".join(prefix_parts)
        return [
            RepositoryEntry(
                path=f"{prefix}/{entry.path}" if prefix else entry.path,
                entry_type=entry.type,
                sha=entry.sha,
                size=entry.size,
            )
            for entry in dto.tree
        ]
