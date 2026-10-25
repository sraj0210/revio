"""Core-side, guarded executor for agent-mode read-only tools.

The model never fetches anything itself: it asks, and this executor decides. Every tool call is
validated and bounded here, in the provider-neutral core, before touching the SCM content port —
path traversal is rejected, a secret deny-list is enforced, and per-file/aggregate byte and
tool-call/file-count budgets are capped. Reaching a budget marks the review context incomplete
rather than crawling further. Fetched content is only ever returned to the model as an untrusted
tool result; it never changes which tools exist or what is allowed.
"""

import fnmatch
import json
import posixpath
from dataclasses import dataclass, field

from revio.domain.agent import AgentToolRequest, AgentToolResult
from revio.domain.models import AgentProvenance, ChangeRequestTarget, ConsultedFile
from revio.ports.scm import RepositoryContentReadPort

# Paths that are never returned, even when explicitly requested. Matched against the full normalized
# path and each path segment. Conservative by design: refusing a safe file is acceptable; returning
# a secret is not.
DEFAULT_SECRET_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "*.keystore",
    "*.jks",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.ppk",
    "credentials*",
    "secrets*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".htpasswd",
)
# Any path with one of these as a segment is refused outright.
DENY_SEGMENTS: frozenset[str] = frozenset({".git", ".ssh", ".aws", ".gnupg", "secrets"})


@dataclass(frozen=True)
class AgentLimits:
    max_tool_calls: int = 8
    max_files: int = 10
    max_bytes_per_file: int = 65_536
    max_aggregate_bytes: int = 262_144
    max_tree_entries: int = 500


def _new_path_set() -> set[str]:
    return set()


def _new_consulted() -> list[ConsultedFile]:
    return []


def _safe_path(raw: object) -> str | None:
    """Return a normalized repo-relative path, or None if it is unsafe or malformed."""
    if not isinstance(raw, str) or not raw or len(raw) > 1000:
        return None
    if "\x00" in raw or any(ord(ch) < 32 for ch in raw):
        return None
    if "\\" in raw or raw.startswith("/"):
        return None
    # Windows drive / UNC style.
    if len(raw) >= 2 and raw[1] == ":":
        return None
    normalized = posixpath.normpath(raw)
    if normalized == ".":
        return ""
    segments = normalized.split("/")
    if any(segment in {"", "..", "."} for segment in segments):
        return None
    if any(segment in DENY_SEGMENTS for segment in segments):
        return None
    return normalized


def _is_excluded(path: str) -> bool:
    candidates = [path, posixpath.basename(path), *path.split("/")]
    return any(
        fnmatch.fnmatch(candidate, glob)
        for candidate in candidates
        for glob in DEFAULT_SECRET_GLOBS
    )


@dataclass
class AgentToolExecutor:
    """Bounded, guarded executor shared across one agent review's turns."""

    content: RepositoryContentReadPort
    target: ChangeRequestTarget
    ref: str
    limits: AgentLimits = field(default_factory=AgentLimits)
    _calls: int = 0
    _bytes: int = 0
    _files: set[str] = field(default_factory=_new_path_set)
    _consulted: list[ConsultedFile] = field(default_factory=_new_consulted)
    _complete: bool = True

    def provenance(self) -> AgentProvenance:
        return AgentProvenance(consulted=tuple(self._consulted), complete=self._complete)

    @property
    def exhausted(self) -> bool:
        return self._calls >= self.limits.max_tool_calls

    async def execute(self, requests: tuple[AgentToolRequest, ...]) -> tuple[AgentToolResult, ...]:
        results: list[AgentToolResult] = []
        for request in requests:
            results.append(await self._execute_one(request))
        return tuple(results)

    async def _execute_one(self, request: AgentToolRequest) -> AgentToolResult:
        if self._calls >= self.limits.max_tool_calls:
            self._complete = False
            return self._refuse(request, "tool-call budget was exhausted")
        self._calls += 1
        if request.name not in ("get_file", "get_tree"):
            return self._refuse(request, "unknown tool")
        raw = request.arguments.get("path")
        # An empty or root path is a valid request for the repository tree root only.
        if request.name == "get_tree" and raw in (None, "", "."):
            return await self._get_tree(request, "")
        path = _safe_path(raw)
        if path is None:
            return self._refuse(request, "path is invalid")
        if path and _is_excluded(path):
            # Exclusion is a deterministic policy answer, not incomplete context.
            return self._refuse(request, "path is excluded from review")
        if request.name == "get_tree":
            return await self._get_tree(request, path)
        return await self._get_file(request, path)

    async def _get_file(self, request: AgentToolRequest, path: str) -> AgentToolResult:
        if not path:
            return self._refuse(request, "path is required")
        if path not in self._files and len(self._files) >= self.limits.max_files:
            self._complete = False
            return self._refuse(request, "file budget was exhausted")
        content = await self.content.get_file(self.target, path, self.ref)
        if content is None:
            return self._refuse(request, "file was not found")
        encoded = len(content.encode("utf-8"))
        if encoded > self.limits.max_bytes_per_file:
            self._complete = False
            return self._refuse(request, "file is too large to inspect")
        if self._bytes + encoded > self.limits.max_aggregate_bytes:
            self._complete = False
            return self._refuse(request, "aggregate content budget was exhausted")
        self._bytes += encoded
        self._files.add(path)
        self._consulted.append(ConsultedFile(path=path, ref=self.ref, kind="get_file"))
        return AgentToolResult(call_id=request.call_id, ok=True, content=content)

    async def _get_tree(self, request: AgentToolRequest, path: str) -> AgentToolResult:
        tree = await self.content.get_tree(self.target, path, self.ref)
        entries = [
            {"path": entry.path, "type": entry.entry_type}
            for entry in tree.items[: self.limits.max_tree_entries]
        ]
        truncated = len(tree.items) > self.limits.max_tree_entries
        if truncated or not tree.completeness.is_complete:
            self._complete = False
        self._consulted.append(ConsultedFile(path=path or ".", ref=self.ref, kind="get_tree"))
        body = json.dumps({"entries": entries, "truncated": truncated}, separators=(",", ":"))
        return AgentToolResult(call_id=request.call_id, ok=True, content=body)

    def _refuse(self, request: AgentToolRequest, reason: str) -> AgentToolResult:
        return AgentToolResult(call_id=request.call_id, ok=False, content=reason)
