# drl-pipeline plugin

CQL-to-DRL conversion pipeline (source intake, baseline, conversion, approval, API JSON, tests, GitHub PR) packaged as an Agent Plugin. Built from `measureable_content_agents` (`main`).

## Contents

| Path | Purpose |
|---|---|
| `com.github.copilot/agents/` | 10 `drl*` agents; start with `drlFlowOrchestrator` |
| `skills/` | `cqm-drl`, `hedis-drl`, `mips-drl`, `drl-review-rules` |
| `mcp.json` | `local-python` MCP server over stdio (no Docker or manual start) |
| `runtime/` | Read-only scripts, templates, docs and orchestration config |
| `overlay/` | Plugin-specific versions of source files; applied over the synced copy |
| `scripts/sync-from-source.ps1` | Rebuilds the plugin from a `measureable_content_agents` checkout |

## Requirements

- Python with `uv` (`python -m uv --version`).
- Environment variables in the shell that launches VS Code: `ADO_PAT`, `GITHUB_TOKEN`, `API_AUTH_BASIC_TOKEN`; `LIBRARIAN_API_KEY` and `LIBRARIAN_API_SECRET` if used.
- Recommended: a local `rules-engine` checkout, with `RULES_ENGINE_REPO_PATH` pointing to it.
- The `ado-content` and `github-content` MCP servers are not bundled; configure them yourself.

## Install

Run **Chat: Install Plugin From Source** in VS Code with this repo's URL, then select `drlFlowOrchestrator` and give it a ticket ID or work-item URL.

Mutable state and artifacts are written to `${PLUGIN_DATA}/drl-pipeline/`, not the install folder.

## Updating from source

```powershell
git clone https://healthcatalyst@dev.azure.com/healthcatalyst/MeasureAble%202.0/_git/measureable_content_agents
.\scripts\sync-from-source.ps1 -SourcePath .\measureable_content_agents
```

A warning means a source file that has an overlay version changed. Merge the change into `overlay/`, then run again with `-UpdateBaseline`. Bump `version` in `plugin.json` before publishing so users receive the update.
