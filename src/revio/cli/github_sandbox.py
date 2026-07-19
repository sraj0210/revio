"""Read-only GitHub sandbox validation command."""

import argparse
import asyncio
import json
import sys

from revio.adapters.scm.github import GITHUB_PROVIDER_ID
from revio.adapters.scm.github.composition import compose_github
from revio.adapters.scm.github.validation import (
    validate_coordinate,
    validate_positive_identifier,
    validate_ref,
    validate_repository_path,
)
from revio.config.github import GitHubSettings
from revio.domain.identifiers import ChangeRequestTarget, InstallationRef, RepositoryRef
from revio.errors import RevioError


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate read-only GitHub sandbox access")
    parser.add_argument("--installation-id", type=_positive, required=True)
    parser.add_argument("--repository", required=True, help="OWNER/NAME")
    parser.add_argument("--pull-request", type=_positive, required=True)
    parser.add_argument("--path", help="Optional file path; contents are never printed")
    parser.add_argument("--ref", help="Explicit ref for file/tree validation")
    parser.add_argument("--tree-path", default="")
    return parser


async def run_validation(args: argparse.Namespace, settings: GitHubSettings) -> int:
    if settings.environment not in {"local", "sandbox"}:
        raise ValueError("sandbox validation CLI requires local or sandbox environment")
    if not settings.github_enabled:
        raise ValueError("GitHub adapter is not enabled")
    owner, separator, name = args.repository.partition("/")
    if not separator or "/" in name:
        raise ValueError("repository must be OWNER/NAME")
    owner = validate_coordinate(owner, "owner")
    name = validate_coordinate(name, "name")
    validate_positive_identifier(args.installation_id, "installation identity")
    validate_positive_identifier(args.pull_request, "pull request number")
    if args.path is not None:
        validate_repository_path(args.path)
    validate_repository_path(args.tree_path, allow_empty=True)
    if args.ref is not None:
        validate_ref(args.ref)

    installation = InstallationRef(
        provider_id=GITHUB_PROVIDER_ID, external_id=str(args.installation_id)
    )
    repository = RepositoryRef(
        installation=installation, external_id=f"sandbox:{owner}/{name}", owner=owner, name=name
    )
    target = ChangeRequestTarget(repository=repository, external_number=args.pull_request)
    composition = compose_github(settings)
    try:
        adapter = composition.adapter
        change = await adapter.get_change_request(target)
        files = await adapter.get_diff(target)
        explicit_ref = args.ref or change.head_sha
        report: dict[str, object] = {
            "pull_request": args.pull_request,
            "repository": f"{owner}/{name}",
            "state": change.state,
            "draft": change.draft,
            "base_sha": change.base_sha,
            "head_sha": change.head_sha,
            "changed_files_completeness": files.completeness.status,
            "changed_files": [
                {
                    "path": item.new_path or item.old_path,
                    "status": item.status,
                    "patch_state": item.patch_state,
                }
                for item in files.items
            ],
        }
        if args.path:
            content = await adapter.get_file(target, args.path, explicit_ref)
            report["file"] = {
                "path": args.path,
                "ref": explicit_ref,
                "found": content is not None,
                "utf8_bytes": len(content.encode()) if content is not None else None,
            }
        tree = await adapter.get_tree(target, args.tree_path, explicit_ref)
        report["tree"] = {
            "ref": explicit_ref,
            "path": args.tree_path,
            "completeness": tree.completeness.status,
            "entry_count": len(tree.items),
            "entries": [{"path": entry.path, "type": entry.entry_type} for entry in tree.items],
        }
        print(json.dumps(report, indent=2))
    finally:
        await composition.close()
    return 0


def main() -> int:
    args = _parser().parse_args()
    try:
        return asyncio.run(run_validation(args, GitHubSettings()))
    except (RevioError, ValueError, OSError):
        print("Revio GitHub validation failed safely", file=sys.stderr)
        return 1
