"""Read-only GitHub implementation of SCM read ports."""

import base64
import binascii
from typing import Literal, cast
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError
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
    GitHubAmbiguousNotFoundError,
    GitHubInvalidRefError,
    GitHubNotFoundError,
    GitHubResponseError,
    GitHubUnsupportedObjectError,
)
from revio.adapters.scm.github.pagination import collect_pages
from revio.adapters.scm.github.validation import (
    validate_coordinate,
    validate_positive_identifier,
    validate_ref,
    validate_repository_path,
)
from revio.domain.identifiers import ChangeRequestTarget
from revio.domain.models import (
    ChangeRequest,
    CollectionCompleteness,
    DiffCollection,
    DiffFile,
    DiffLine,
    RepositoryEntry,
    TreeCollection,
)


def _validated_dto[T: BaseModel](model: type[T], response: httpx.Response) -> T | None:
    try:
        return model.model_validate(response.json())
    except (ValidationError, ValueError, TypeError):
        return None


def _validated_file_list(response: httpx.Response) -> list[GitHubFileDTO] | None:
    try:
        raw = cast(object, response.json())
        if not isinstance(raw, list):
            return None
        payload = cast(list[object], raw)
        return [GitHubFileDTO.model_validate(item) for item in payload]
    except (ValidationError, ValueError, TypeError):
        return None


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
        except ValueError:
            raise GitHubResponseError("invalid installation identity") from None
        return (
            validate_positive_identifier(installation_id, "installation identity"),
            validate_coordinate(repository.owner, "owner"),
            validate_coordinate(repository.name, "name"),
        )

    @staticmethod
    def _repo_path(owner: str, repo: str) -> str:
        return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"

    async def _pull_request_dto(
        self, target: ChangeRequestTarget
    ) -> tuple[GitHubPullRequestDTO, int, str]:
        installation, owner, repo = self._coordinates(target)
        root = self._repo_path(owner, repo)
        response = await self._client.get(installation, f"{root}/pulls/{target.external_number}")
        dto = _validated_dto(GitHubPullRequestDTO, response)
        if dto is None:
            raise GitHubResponseError("invalid GitHub pull request response") from None
        return dto, installation, root

    async def get_change_request(self, target: ChangeRequestTarget) -> ChangeRequest:
        dto, _, _ = await self._pull_request_dto(target)
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

    async def get_diff(self, target: ChangeRequestTarget) -> DiffCollection:
        pull_request, installation, root = await self._pull_request_dto(target)

        def parse(response: httpx.Response) -> list[GitHubFileDTO]:
            parsed = _validated_file_list(response)
            if parsed is None:
                raise GitHubResponseError("invalid GitHub changed-files response") from None
            return parsed

        items, completeness = await collect_pages(
            self._client,
            installation,
            f"{root}/pulls/{target.external_number}/files",
            params={"per_page": 100},
            parse=parse,
            max_pages=self._max_pages,
            max_items=self._max_items,
        )
        if (
            completeness.is_complete
            and pull_request.changed_files is not None
            and len(items) < pull_request.changed_files
        ):
            completeness = CollectionCompleteness(status="provider_truncated")
        return DiffCollection(
            items=tuple(self._map_file(item) for item in items), completeness=completeness
        )

    @staticmethod
    def _map_file(item: GitHubFileDTO) -> DiffFile:
        status_map: dict[str, Literal["added", "modified", "deleted", "renamed", "copied"]] = {
            "added": "added",
            "modified": "modified",
            "removed": "deleted",
            "renamed": "renamed",
            "copied": "copied",
            "changed": "modified",
            "unchanged": "modified",
        }
        status = status_map[item.status]
        if status == "added":
            old_path, new_path = None, item.filename
        elif status == "deleted":
            old_path, new_path = item.filename, None
        elif status in {"renamed", "copied"}:
            old_path, new_path = item.previous_filename, item.filename
        else:
            old_path = new_path = item.filename

        lines: list[DiffLine] = []
        patch_state: Literal[
            "complete", "missing", "malformed", "provider_truncated", "binary_or_no_textual_patch"
        ] = "complete"
        if item.patch is None:
            if item.changes == 0:
                patch_state = "binary_or_no_textual_patch"
            elif item.changes is not None:
                patch_state = "provider_truncated"
            else:
                patch_state = "missing"
        else:
            try:
                source = f"a/{old_path}" if old_path is not None else "/dev/null"
                target = f"b/{new_path}" if new_path is not None else "/dev/null"
                patch = PatchSet(f"--- {source}\n+++ {target}\n{item.patch}")
                saw_hunk = False
                for patched_file in patch:
                    for hunk in patched_file:
                        saw_hunk = True
                        for line in hunk:
                            value = line.value.rstrip("\n")
                            if line.is_added:
                                lines.append(
                                    DiffLine(
                                        content=value, side="new", new_line=line.target_line_no
                                    )
                                )
                            elif line.is_removed:
                                lines.append(
                                    DiffLine(
                                        content=value, side="old", old_line=line.source_line_no
                                    )
                                )
                            elif line.is_context:
                                lines.append(
                                    DiffLine(
                                        content=value,
                                        side="context",
                                        old_line=line.source_line_no,
                                        new_line=line.target_line_no,
                                    )
                                )
                if not saw_hunk:
                    patch_state = "malformed"
            except (UnidiffParseError, ValueError, TypeError):
                patch_state = "malformed"
                lines = []
        return DiffFile(
            old_path=old_path,
            new_path=new_path,
            status=status,
            lines=tuple(lines),
            patch_state=patch_state,
        )

    async def _verify_repository_and_ref(self, installation: int, root: str, ref: str) -> str:
        validated_ref = validate_ref(ref)
        try:
            await self._client.get(installation, root)
        except GitHubNotFoundError:
            raise GitHubAmbiguousNotFoundError(
                "GitHub repository is unavailable or inaccessible"
            ) from None
        try:
            response = await self._client.get(
                installation, f"{root}/commits/{quote(validated_ref, safe='')}"
            )
        except GitHubNotFoundError:
            raise GitHubInvalidRefError("GitHub ref not found") from None
        return self._commit_tree_sha(response)

    @staticmethod
    def _commit_tree_sha(response: httpx.Response) -> str:
        tree_sha = GitHubReadAdapter._extract_tree_sha(response)
        if tree_sha is None:
            raise GitHubResponseError("invalid GitHub commit response") from None
        return tree_sha

    @staticmethod
    def _extract_tree_sha(response: httpx.Response) -> str | None:
        try:
            tree_sha = response.json()["commit"]["tree"]["sha"]
            if not isinstance(tree_sha, str) or not tree_sha:
                return None
            return tree_sha
        except (KeyError, TypeError, ValueError):
            return None

    async def get_file(self, target: ChangeRequestTarget, path: str, ref: str) -> str | None:
        installation, owner, repo = self._coordinates(target)
        parts = validate_repository_path(path)
        root = self._repo_path(owner, repo)
        await self._verify_repository_and_ref(installation, root, ref)
        encoded_path = "/".join(quote(part, safe="") for part in parts)
        try:
            response = await self._client.get(
                installation, f"{root}/contents/{encoded_path}", params={"ref": ref}
            )
        except GitHubNotFoundError:
            return None
        dto = _validated_dto(GitHubContentDTO, response)
        if dto is None:
            raise GitHubResponseError("invalid GitHub content response") from None
        if dto.type != "file":
            raise GitHubUnsupportedObjectError("GitHub object is not a file")
        if dto.encoding != "base64" or dto.content is None:
            raise GitHubUnsupportedObjectError("unsupported GitHub file encoding")
        if dto.size is not None and dto.size > self._max_file_bytes:
            raise GitHubResponseError("GitHub file exceeds configured limit")
        try:
            raw = base64.b64decode("".join(dto.content.split()), validate=True)
        except (ValueError, binascii.Error):
            raise GitHubResponseError("invalid GitHub file content") from None
        if len(raw) > self._max_file_bytes:
            raise GitHubResponseError("GitHub file exceeds configured limit")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise GitHubUnsupportedObjectError("GitHub file is not UTF-8 text") from None

    async def get_tree(self, target: ChangeRequestTarget, path: str, ref: str) -> TreeCollection:
        installation, owner, repo = self._coordinates(target)
        root = self._repo_path(owner, repo)
        tree_sha = await self._verify_repository_and_ref(installation, root, ref)
        segments = validate_repository_path(path, allow_empty=True)
        prefix_parts: list[str] = []
        dto: GitHubTreeDTO | None = None
        for request_number in range(len(segments) + 1):
            if request_number >= self._max_pages:
                return TreeCollection(
                    items=(),
                    completeness=CollectionCompleteness(status="service_page_limit"),
                )
            response = await self._client.get(
                installation, f"{root}/git/trees/{quote(tree_sha, safe='')}"
            )
            dto = _validated_dto(GitHubTreeDTO, response)
            if dto is None:
                raise GitHubResponseError("invalid GitHub tree response") from None
            if request_number == len(segments):
                break
            segment = segments[request_number]
            child = next((entry for entry in dto.tree if entry.path == segment), None)
            if child is None:
                return TreeCollection()
            if child.type != "tree":
                raise GitHubUnsupportedObjectError("GitHub tree path is not a directory")
            tree_sha = child.sha
            prefix_parts.append(segment)
        assert dto is not None
        status: Literal["complete", "provider_truncated", "service_item_limit"] = "complete"
        entries = dto.tree
        if dto.truncated:
            status = "provider_truncated"
        if len(entries) > self._max_items:
            entries = entries[: self._max_items]
            status = "service_item_limit"
        prefix = "/".join(prefix_parts)
        return TreeCollection(
            items=tuple(
                RepositoryEntry(
                    path=f"{prefix}/{entry.path}" if prefix else entry.path,
                    entry_type=entry.type,
                    sha=entry.sha,
                    size=entry.size,
                )
                for entry in entries
            ),
            completeness=CollectionCompleteness(status=status),
        )
