#!/usr/bin/env python3
"""Extraction-stage non-MCP Azure DevOps fallback helper.

Purpose:
- Keep MCP as the preferred path for ADO interactions.
- Provide a deterministic fallback for extraction-stage gaps only.

Supported operations:
1) Fetch raw CQL file contents from authoritative intake cqlPaths at a PR source commit.
2) Fetch raw CQL file contents and build normalized extraction JSON.

Out of scope by design:
- Ticket and PR metadata resolution
- PR iteration changed-file discovery
- Pipeline stage transitions
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Add scripts dir to path for shared_utils import  
sys.path.insert(0, str(Path(__file__).parent))
from shared_utils import load_json, write_json, auth_header_from_env


ASSUMPTIONS = [
    "MCP was used for PR metadata resolution and source commit enforcement.",
    "HTTPS fallback was used only for latest PR iteration changed files and commit-path file content retrieval.",
    "Extraction is syntax-level and does not infer unstated business logic.",
]


INCLUDE_RE = re.compile(
    r'^\s*include\s+"([^"]+)"(?:\s+version\s+(?:\'([^\']+)\'|"([^"]+)"))?\s+called\s+([A-Za-z_][A-Za-z0-9_]*)',
    re.IGNORECASE,
)
DEFINE_RE = re.compile(
    r'^\s*define\s+(?:function\s+)?"([^"]+)"(?:\s*\([^)]*\))?\s*:?',
    re.IGNORECASE,
)
TERMINOLOGY_RE = re.compile(
    r'^\s*(valueset|codesystem|code|concept)\s+"([^"]+)"(?:\s*:\s*(?:\'([^\']+)\'|"([^"]+)"))?',
    re.IGNORECASE,
)
TRUNCATION_MARKERS = (
    "truncated for brevity",
    "omitted for brevity",
    "summarized for brevity",
)


def _parse_ticket_id_from_url(ticket_url: str) -> str:
    match = re.search(r"/edit/(\d+)", ticket_url)
    if not match:
        raise RuntimeError(f"Could not parse ticket id from ticketUrl: {ticket_url}")
    return match.group(1)


def _infer_ticket_id(explicit_ticket_id: str | None) -> str:
    if explicit_ticket_id:
        return explicit_ticket_id

    run_input_path = Path("state/run-input.json")
    if not run_input_path.exists():
        raise RuntimeError("Ticket id not provided and state/run-input.json not found.")

    run_input = load_json(run_input_path)

    ticket_url = str(run_input.get("ticketUrl", "")).strip()
    if ticket_url:
        return _parse_ticket_id_from_url(ticket_url)

    raise RuntimeError("Could not infer ticket id from run-input.json.")


def _http_get_bytes(url: str, auth_header: str) -> bytes:
    req = urllib.request.Request(url)
    req.add_header("Authorization", auth_header)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def _validate_raw_cql(content: bytes) -> tuple[bool, str, str]:
    if not content.strip():
        return False, "downloaded content is empty", ""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return False, f"downloaded content is not UTF-8 decodable: {exc}", ""
    if any(marker in text.lower() for marker in TRUNCATION_MARKERS):
        return False, "downloaded content contains a truncation marker", text
    return True, "", text


def _build_item_url(project_url: str, repository: str, path: str, commit_id: str) -> str:
    encoded_path = urllib.parse.quote(path, safe="/")
    encoded_commit = urllib.parse.quote(commit_id, safe="")
    return (
        f"{project_url}/_apis/git/repositories/{repository}/items"
        f"?path={encoded_path}"
        f"&versionDescriptor.versionType=commit"
        f"&versionDescriptor.version={encoded_commit}"
        "&download=true"
        "&resolveLfs=true"
        "&api-version=7.1"
    )


def _canonical_measure_slug_from_cql_path(source_cql_path: str, family: str) -> str:
    """Derive the canonical DRL measure slug from a CQL path/basename.

    Mirrors the rules-engine convention so patch filenames line up with DRL filenames:
    strips a trailing 'ADM' suffix, family-prefixes numeric-only stems, and otherwise
    keeps the lowercase stem (e.g. CMS138Rate1ADM.cql + cms -> cms138rate1; TRCA.cql -> trca).
    """
    stem = Path(str(source_cql_path or "").strip()).stem.strip().lower()
    fam = str(family or "").strip().lower()
    if not stem:
        return ""
    if stem.endswith("adm"):
        stem = stem[:-3]
    if fam and stem.startswith(fam):
        return stem
    numeric = re.match(r"^(\d+)(.*)$", stem)
    if numeric:
        return f"{fam}{numeric.group(1).zfill(3)}{numeric.group(2)}" if fam else f"{numeric.group(1).zfill(3)}{numeric.group(2)}"
    return stem


def _fetch_pr_base_commit(project_url: str, repository: str, pr_id: str, auth_header: str) -> tuple[str, str]:
    """Return (baseCommitId, error) for a pull request.

    The base is the PR's lastMergeTargetCommit — the target-branch state the PR branched
    from. Diffing CQL at this commit against the PR head yields the PR's own changes,
    regardless of whether the PR is active or already completed/merged. baseCommitId is
    '' on error.
    """
    encoded_repo = urllib.parse.quote(repository, safe="")
    encoded_pr = urllib.parse.quote(pr_id, safe="")
    url = (
        f"{project_url}/_apis/git/repositories/{encoded_repo}"
        f"/pullrequests/{encoded_pr}?api-version=7.1"
    )
    try:
        raw = _http_get_bytes(url, auth_header)
        data = json.loads(raw.decode("utf-8-sig", errors="replace"))
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"
    base = str((data.get("lastMergeTargetCommit") or {}).get("commitId", "")).strip()
    return base, "" if base else "lastMergeTargetCommit missing"


def _generate_cql_pr_diffs(
    *,
    intake: dict[str, Any],
    payload: dict[str, Any],
    patches_dir: Path,
) -> list[dict[str, Any]]:
    """Write the PR's own CQL change as a unified diff patch per changed CQL file.

    For each changed CQL file the new content is the already-downloaded PR file (head) and
    the old content is the same path fetched at the PR base commit. The diff is therefore
    the actual change the PR introduced — it is never empty (and never depends on whether
    the PR is completed/merged). A file the PR newly added (base 404) diffs against empty,
    so every new line is an added/green line.
    """
    project_url = str(intake.get("projectUrl", "")).rstrip("/")
    family = str(intake.get("measureFamily", "")).strip().lower()
    auth_header = auth_header_from_env()
    patches_dir.mkdir(parents=True, exist_ok=True)

    base_commit_by_pr: dict[str, str] = {}
    diffs: list[dict[str, Any]] = []

    for pr in payload.get("resolvedPullRequests", []):
        pr_id = str(pr.get("prId", "")).strip()
        repository = str(pr.get("repository", "")).strip()
        if pr_id and pr_id not in base_commit_by_pr:
            base_commit, _err = _fetch_pr_base_commit(project_url, repository, pr_id, auth_header)
            base_commit_by_pr[pr_id] = base_commit
        base_commit = base_commit_by_pr.get(pr_id, "")

        for raw_file in pr.get("rawFiles", []):
            source_path = str(raw_file.get("path", "")).strip()
            raw_path_rel = str(raw_file.get("rawFile", "")).strip()
            if not source_path or not raw_path_rel:
                continue
            raw_path = Path(raw_path_rel)
            if not raw_path.exists():
                continue
            slug = _canonical_measure_slug_from_cql_path(source_path, family) or Path(source_path).stem.lower()
            new_text = raw_path.read_text(encoding="utf-8-sig", errors="replace")

            old_text = ""
            base_status = "added"
            if base_commit:
                url = _build_item_url(project_url, repository, source_path, base_commit)
                try:
                    old_text = _http_get_bytes(url, auth_header).decode("utf-8-sig", errors="replace")
                    base_status = "edited"
                except urllib.error.HTTPError as exc:
                    # 404 = the PR added this file (absent at base); whole file is the real change.
                    old_text = ""
                    base_status = "added" if exc.code == 404 else "base-unavailable"
                except Exception:
                    old_text = ""
                    base_status = "base-unavailable"

            diff_lines = list(
                difflib.unified_diff(
                    old_text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile=f"{source_path}@{base_commit[:8] or 'base'}",
                    tofile=f"{source_path}@head",
                )
            )
            patch_text = "".join(diff_lines)
            added = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
            removed = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))
            patch_file = patches_dir / f"{slug}-cql-diff.patch"
            patch_file.write_text(patch_text, encoding="utf-8")
            diffs.append(
                {
                    "slug": slug,
                    "path": source_path,
                    "patchFile": patch_file.as_posix(),
                    "baseCommit": base_commit,
                    "baseStatus": base_status,
                    "addedLines": added,
                    "removedLines": removed,
                }
            )
    return diffs


def _candidate_commit_ids(row: dict[str, Any]) -> list[tuple[str, str]]:
    # Fetch the CHANGED CQL at the PR head (source branch tip = lastMergeSourceCommit),
    # which is the only commit guaranteed to contain the PR's edits. `mergeCommit` is
    # populated from lastMergeTargetCommit (the target/base the PR branched from) and does
    # NOT contain the change, so it is only a last-resort fallback. Source commits remain
    # fetchable by id even after the source branch is deleted post-merge.
    candidates = [
        ("sourceCommit", str(row.get("sourceCommit", "")).strip()),
        ("sourceCommitId", str(row.get("sourceCommitId", "")).strip()),
        ("mergeCommit", str(row.get("mergeCommit", "")).strip()),
    ]
    seen: set[str] = set()
    unique_candidates: list[tuple[str, str]] = []
    for label, commit_id in candidates:
        if commit_id and commit_id not in seen:
            unique_candidates.append((label, commit_id))
            seen.add(commit_id)
    return unique_candidates


def _raw_output_path(raw_dir: Path, source_path: str, seen_names: set[str] | None = None) -> Path:
    """Map source CQL path to a flat filename under raw_dir.

    Keeps only filename-level outputs (no nested directories) and adds a short
    suffix when duplicate basenames appear.
    """
    source = str(source_path or "").strip()
    basename = Path(source).name.strip() if source else ""
    if not basename:
        basename = source.strip("/").replace("/", "-").replace("\\", "-") or "source.cql"

    if not basename.lower().endswith(".cql"):
        basename = f"{basename}.cql"

    candidate_name = basename
    if seen_names is not None:
        if candidate_name in seen_names:
            digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:8]
            stem = Path(basename).stem
            suffix = Path(basename).suffix or ".cql"
            candidate_name = f"{stem}-{digest}{suffix}"
        seen_names.add(candidate_name)

    return raw_dir / candidate_name


def _normalize_resolved_prs(resolved_prs: Any) -> list[dict[str, Any]]:
    """Coerce intake.resolvedPullRequests into a list of PR row dicts.

    Agents sometimes store the raw get_pr_changes return blob (a single dict with keys
    like ok/returnCode/cqlPaths/sourceCommit/mergeCommit) instead of the expected list of
    PR rows. Iterating that dict yields its string keys and later .get() calls crash with
    'str' object has no attribute 'get'. Normalize defensively so a single dict is wrapped
    into a one-element list, and surface a clear, actionable error when a usable PR id is
    absent rather than failing with a cryptic AttributeError.
    """
    if isinstance(resolved_prs, dict):
        rows = [resolved_prs]
    elif isinstance(resolved_prs, list):
        rows = [r for r in resolved_prs if isinstance(r, dict)]
        bad = [r for r in resolved_prs if not isinstance(r, dict)]
        if bad and not rows:
            raise RuntimeError(
                "intake.resolvedPullRequests must be a list of PR objects, but it "
                f"contained only non-object entries (e.g. {bad[0]!r}). Re-run intake so "
                "each pull request is written as an object with prId/repository/sourceCommit."
            )
    else:
        raise RuntimeError(
            "intake.resolvedPullRequests must be a list of PR objects or a single PR "
            f"object, got {type(resolved_prs).__name__}."
        )
    for row in rows:
        pr_id = str(row.get("id", "") or row.get("pullRequestId", "") or row.get("prId", "")).strip()
        if not pr_id:
            raise RuntimeError(
                "intake.resolvedPullRequests is missing a pull-request id (prId/"
                "pullRequestId/id). The raw get_pr_changes blob does not include the PR id; "
                "intake must record the resolved PR as an object that includes prId. "
                f"Offending entry keys: {sorted(row.keys())}."
            )
    return rows


def _fetch_to_raw_and_manifest(
    *,
    ticket_id: str,
    intake: dict[str, Any],
    raw_dir: Path,
    manifest_path: Path,
) -> tuple[dict[str, Any], int, int]:
    project_url = str(intake.get("projectUrl", "")).rstrip("/")
    if not project_url:
        raise RuntimeError("intake.projectUrl is required.")

    cql_paths = [str(path).strip() for path in intake.get("cqlPaths", []) if str(path).strip()]
    if not cql_paths:
        raise RuntimeError("No cqlPaths found in intake artifact.")

    resolved_prs = intake.get("resolvedPullRequests", [])
    if not resolved_prs:
        raise RuntimeError("No resolvedPullRequests found in intake artifact.")
    resolved_prs = _normalize_resolved_prs(resolved_prs)

    auth_header = auth_header_from_env()
    pending_paths = list(cql_paths)
    resolved_rows: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    invalid_raw_files: list[dict[str, str]] = []
    fetch_errors: list[dict[str, str]] = []

    for row in resolved_prs:
        pr_id = str(row.get("id", "") or row.get("pullRequestId", "") or row.get("prId", "")).strip()
        repository = str(row.get("repositoryId", "") or row.get("repository", "") or row.get("repositoryName", "")).strip()
        repository_name = str(row.get("repository", "") or row.get("repositoryName", "")).strip()
        source_ref_name = str(row.get("sourceRefName", "") or row.get("branch", "") or row.get("sourceBranch", "")).strip()
        
        commit_candidates = _candidate_commit_ids(row)
        if not pr_id or not repository or not commit_candidates:
            continue

        fetched_cql_paths: list[str] = []
        raw_files: list[dict[str, str]] = []

        for cql_path in list(pending_paths):
            content: bytes | None = None
            fetched_commit_id = ""
            fetched_commit_label = ""
            last_error = ""
            for candidate_commit_label, candidate_commit_id in commit_candidates:
                item_url = _build_item_url(
                    project_url=project_url,
                    repository=repository,
                    path=cql_path,
                    commit_id=candidate_commit_id,
                )
                try:
                    content = _http_get_bytes(item_url, auth_header)
                    fetched_commit_id = candidate_commit_id
                    fetched_commit_label = candidate_commit_label
                    break
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"

            if content is None:
                fetch_errors.append(
                    {
                        "path": cql_path,
                        "repository": repository,
                        "commitIdsTried": ", ".join(commit_id for _, commit_id in commit_candidates),
                        "error": last_error,
                    }
                )
                continue

            is_valid, invalid_reason, text = _validate_raw_cql(content)
            if not is_valid:
                invalid_raw_files.append(
                    {
                        "path": cql_path,
                        "reason": invalid_reason,
                    }
                )
                continue

            output_path = _raw_output_path(raw_dir, cql_path, seen_names)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(content)

            fetched_cql_paths.append(cql_path)
            raw_files.append(
                {
                    "path": cql_path,
                    "fetchedCommitId": fetched_commit_id,
                    "fetchedCommitSource": fetched_commit_label,
                    "rawFile": output_path.as_posix(),
                    "bytes": len(content),
                    "lines": text.count("\n") + (1 if text else 0),
                }
            )
            pending_paths.remove(cql_path)

        resolved_rows.append(
            {
                "prId": pr_id,
                "repositoryId": str(row.get("repositoryId", "")).strip() or None,
                "repository": repository_name,
                "sourceRefName": source_ref_name,
                "commitCandidates": [
                    {"source": label, "commitId": commit_id}
                    for label, commit_id in commit_candidates
                ],
                "usedCommitIds": sorted(
                    {str(raw_file.get("fetchedCommitId", "")) for raw_file in raw_files if raw_file.get("fetchedCommitId")}
                ),
                "changedCqlPaths": fetched_cql_paths,
                "rawFiles": raw_files,
            }
        )

    payload = {
        "ticketId": ticket_id,
        "method": "mcp-first-with-https-fallback",
        "resolvedPullRequests": resolved_rows,
        "unresolvedCqlPaths": pending_paths,
        "invalidRawFiles": invalid_raw_files,
        "fetchErrors": fetch_errors,
        "expectedCount": len(cql_paths),
        "fetchedCount": len(cql_paths) - len(pending_paths),
        "unresolvedCount": len(pending_paths),
    }
    write_json(manifest_path, payload)
    return payload, len(cql_paths) - len(pending_paths), len(pending_paths)


def _parse_cql_text(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    definitions: list[dict[str, Any]] = []
    includes: list[dict[str, Any]] = []
    terminology: list[dict[str, Any]] = []

    for line_number, line in enumerate(text.splitlines(), start=1):
        include_match = INCLUDE_RE.match(line)
        if include_match:
            includes.append(
                {
                    "library": include_match.group(1),
                    "version": include_match.group(2) or include_match.group(3),
                    "alias": include_match.group(4),
                    "line": line_number,
                }
            )

        define_match = DEFINE_RE.match(line)
        if define_match:
            definitions.append(
                {
                    "name": define_match.group(1),
                    "line": line_number,
                }
            )

        term_match = TERMINOLOGY_RE.match(line)
        if term_match:
            terminology.append(
                {
                    "kind": term_match.group(1).lower(),
                    "name": term_match.group(2),
                    "identifier": term_match.group(3) or term_match.group(4),
                    "line": line_number,
                }
            )

    return definitions, includes, terminology


def run_download_and_extract(args: argparse.Namespace) -> int:
    ticket_id = _infer_ticket_id(args.ticket_id)
    intake_path = Path(args.intake) if args.intake else Path(f"artifacts/{ticket_id}/intake.json")
    if not intake_path.exists():
        raise FileNotFoundError(f"Missing intake artifact: {intake_path}")

    intake = load_json(intake_path)
    raw_dir = Path(args.raw_dir) if args.raw_dir else Path(f"artifacts/{ticket_id}/cqls/extraction")
    manifest_path = Path(args.out) if args.out else Path(f"artifacts/{ticket_id}/extraction-fetched.json")
    extraction_path = Path(args.extraction_out) if args.extraction_out else Path(f"artifacts/{ticket_id}/extraction.json")

    payload, fetched_count, unresolved_count = _fetch_to_raw_and_manifest(
        ticket_id=ticket_id,
        intake=intake,
        raw_dir=raw_dir,
        manifest_path=manifest_path,
    )

    files: list[dict[str, Any]] = []
    for pr in payload.get("resolvedPullRequests", []):
        pr_id = str(pr.get("prId", "")).strip()
        repository = str(pr.get("repository", "")).strip()
        for raw_file in pr.get("rawFiles", []):
            source_path = str(raw_file.get("path", "")).strip()
            raw_path_rel = str(raw_file.get("rawFile", "")).strip()
            if not source_path or not raw_path_rel:
                continue

            raw_path = Path(raw_path_rel)
            if not raw_path.exists():
                continue

            cql_text = raw_path.read_text(encoding="utf-8-sig")
            definitions, includes, terminology = _parse_cql_text(cql_text)
            files.append(
                {
                    "prId": pr_id,
                    "repository": repository,
                    "path": source_path,
                    "sourceCommitId": str(raw_file.get("fetchedCommitId", "")).strip(),
                    "definitions": definitions,
                    "includes": includes,
                    "terminology": terminology,
                }
            )

    if not files:
        print(f"ticketId={ticket_id}")
        print(f"fetched={fetched_count}")
        print(f"unresolved={unresolved_count}")
        print("extraction=none")
        return 1

    if unresolved_count or payload.get("invalidRawFiles"):
        print(f"ticketId={ticket_id}")
        print(f"fetched={fetched_count}")
        print(f"unresolved={unresolved_count}")
        print(f"invalidRawFiles={len(payload.get('invalidRawFiles', []))}")
        print("extraction=blocked")
        return 1

    # Step 1: write the PR's own CQL change as a unified diff patch per changed file
    # (base commit -> head). This is the actual merged change and is never empty; it does
    # not depend on whether the PR is completed/merged.
    cql_pr_diffs = _generate_cql_pr_diffs(
        intake=intake,
        payload=payload,
        patches_dir=Path(f"artifacts/{ticket_id}/patches"),
    )

    extraction_payload = {
        "ticketId": ticket_id,
        "sourceType": "pr-changed-cql-files",
        "files": files,
        "cqlPrDiffs": cql_pr_diffs,
        "assumptions": ASSUMPTIONS,
        "status": "extraction-complete",
    }
    write_json(extraction_path, extraction_payload)

    print(f"ticketId={ticket_id}")
    print(f"fetched={fetched_count}")
    print(f"unresolved={unresolved_count}")
    print(f"manifest={manifest_path.as_posix()}")
    print(f"extraction={extraction_path.as_posix()}")
    print(f"filesParsed={len(files)}")
    print(f"cqlPrDiffs={len(cql_pr_diffs)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extraction-stage non-MCP Azure DevOps fallback helper.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser(
        "download-and-extract",
        help="Fetch raw CQL and build extraction artifact from intake cqlPaths at PR source commit.",
    )
    extract_parser.add_argument(
        "--ticket-id",
        default="",
        help="Ticket id. If omitted, inferred from run-input.json.",
    )
    extract_parser.add_argument(
        "--intake",
        default="",
        help="Path to intake JSON. Default .github/agents/Drl Pipeline/artifacts/<ticket-id>/intake.json",
    )
    extract_parser.add_argument(
        "--raw-dir",
        default="",
        help="Optional output directory for raw CQL files. Default .github/agents/Drl Pipeline/artifacts/<ticket-id>/cqls/extraction",
    )
    extract_parser.add_argument(
        "--out",
        default="",
        help="Optional output path for fetched manifest. Default .github/agents/Drl Pipeline/artifacts/<ticket-id>/extraction-fetched.json",
    )
    extract_parser.add_argument(
        "--extraction-out",
        default="",
        help="Optional output path for extraction JSON. Default .github/agents/Drl Pipeline/artifacts/<ticket-id>/extraction.json",
    )
    extract_parser.set_defaults(func=run_download_and_extract)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as err:  # noqa: BLE001
        print(str(err), file=sys.stderr)
        raise SystemExit(1)