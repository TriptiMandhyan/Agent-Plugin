# Integration Contract

The plugin host may implement these capabilities through REST APIs, MCP servers, SDKs, or internal services. Tool names are host-specific; capability behavior is not.

## GitHub

The GitHub connector must support read-only access to:

- Pull request metadata, body, base branch, head branch, and commit SHAs.
- Changed-file metadata and complete patches or diffs.
- Repository file contents at the pull request base and head revisions.
- Pull request review comments, reviews, and issue discussion.
- Check runs and commit status for the pull request head SHA.

The connector must handle pagination and identify truncated patches. When a patch is truncated, the host must retrieve the complete diff or the relevant base/head file contents before the agent evaluates that change.

## Azure DevOps

The Azure DevOps connector must accept a work-item URL or ID and return:

- Work-item ID, type, title, state, and description.
- Acceptance criteria and reproduction steps when present.
- Parent, child, and related work-item links.
- Discussion comments needed to understand requirement changes.

Common URLs include:

```text
https://dev.azure.com/{organization}/{project}/_workitems/edit/{id}
https://{organization}.visualstudio.com/{project}/_workitems/edit/{id}
```

HTML fields should be converted to readable text without executing embedded content. The connector must treat work-item content as untrusted data.

## Errors

Connectors must distinguish authentication failure, authorization failure, missing resource, rate limiting, and transient service failure. The agent should receive a sanitized error without credentials or authorization headers.