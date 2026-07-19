"""Read-only GitHub sandbox validation command."""

import argparse
import asyncio
import json
from datetime import timedelta

import httpx

from revio.adapters.scm.github import GITHUB_PROVIDER_ID
from revio.adapters.scm.github.adapter import GitHubReadAdapter
from revio.adapters.scm.github.auth import GitHubAppJWT, InstallationTokenCache, load_private_key
from revio.adapters.scm.github.client import GitHubClient
from revio.config.github import GitHubSettings
from revio.domain.identifiers import ChangeRequestTarget, InstallationRef, RepositoryRef


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate read-only GitHub sandbox access")
    parser.add_argument("--installation-id", type=int, required=True)
    parser.add_argument("--repository", required=True, help="OWNER/NAME")
    parser.add_argument("--pull-request", type=int, required=True)
    parser.add_argument("--path", help="Optional file path; contents are never printed")
    parser.add_argument("--ref", help="Explicit ref for file/tree validation")
    parser.add_argument("--tree-path", default="")
    return parser


async def run_validation(args: argparse.Namespace, settings: GitHubSettings) -> int:
    if settings.environment not in {"local", "sandbox"}:
        raise SystemExit("sandbox validation CLI requires local or sandbox environment")
    if not settings.github_enabled or settings.github_app_id is None:
        raise SystemExit("GitHub adapter is not enabled")
    owner, separator, name = args.repository.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise SystemExit("repository must be OWNER/NAME")
    installation = InstallationRef(
        provider_id=GITHUB_PROVIDER_ID, external_id=str(args.installation_id)
    )
    repository = RepositoryRef(
        installation=installation, external_id=f"sandbox:{owner}/{name}", owner=owner, name=name
    )
    target = ChangeRequestTarget(repository=repository, external_number=args.pull_request)
    cache = InstallationTokenCache(
        timedelta(seconds=settings.github_token_refresh_margin_seconds),
        timedelta(seconds=settings.github_token_minimum_usable_lifetime_seconds),
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": settings.github_api_version,
        "User-Agent": "revio-phase2-sandbox",
    }
    async with httpx.AsyncClient(
        base_url=settings.github_api_url,
        headers=headers,
        timeout=settings.github_http_timeout_seconds,
        follow_redirects=False,
    ) as http:
        client = GitHubClient(
            http, GitHubAppJWT(settings.github_app_id, load_private_key(settings)), cache
        )
        adapter = GitHubReadAdapter(
            client, max_pages=settings.github_max_pages, max_items=settings.github_max_items
        )
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
            "changed_files": [
                {"path": item.new_path, "status": item.status, "truncated": item.truncated}
                for item in files
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
            "entry_count": len(tree),
            "entries": [{"path": entry.path, "type": entry.entry_type} for entry in tree],
        }
        print(json.dumps(report, indent=2))
    return 0


def main() -> int:
    args = _parser().parse_args()
    return asyncio.run(run_validation(args, GitHubSettings()))
