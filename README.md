# drl-pipeline plugin

CQL-to-DRL conversion pipeline (source intake, baseline, conversion, approval, API JSON, tests, GitHub PR) packaged as an Agent Plugin. Built from `measureable_content_agents` (`main`).

## How it works

You give `drlFlowOrchestrator` an Azure DevOps ticket ID or work-item URL. It reads the pipeline stage and runs the matching agent, one stage at a time:

| Stage | Agent | What it does |
|---|---|---|
| 1. Source intake | `drlSourceIntake` | Reads the ticket, fetches the CQL and its diff, writes `intake.json`, `extraction.json`, `plan.md`. Stops early if the DRL already has the change. |
| 2. Baseline | `drlBaselineBuilder` | Fetches the prior-year DRL and rolls year tokens forward. |
| 3. Conversion | `drlConverter` | Applies the CQL changes to the baseline, validates, writes the DRL patch and `mapping.md`. |
| 4. Approval | `drlApprovalRecorder` | Records a human approval. **You run this yourself after reviewing the changes.** |
| 5. API JSON | `drlApiBuilder` | Fetches the measure JSON from the authenticated API and writes it to `pr-assets/`. |
| 6. Tests | `drlTestBuilder` | Builds the current-year Java test artifacts. |
| 7. Pull request | `drlPrSubmitter` | Pushes only the approved changes to a remote branch and opens the GitHub PR. It never touches your local `rules-engine` checkout. |

The pipeline pauses after stage 3. Review the generated files (see below), then run `/drlApprovalRecorder` with your name. `drlLearning` and `drlTicketReset` are helpers; reset only when you want a clean rerun.

## Where files are stored

Artifacts are written to a `drl-pipeline/` folder in the project you have open (the MCP server's working directory), so you can open and diff them in the editor:

```
drl-pipeline/
  .gitignore                  contains "*", so git ignores the whole folder
  artifacts/<ticket-id>/
    intake.json, extraction.json, plan.md
    cqls/extraction/          fetched CQL
    drls/baseline/, drls/generated/
    patches/                  CQL diffs and DRL patches
    mapping.md
    state/                    pipeline and approval status
    pr-assets/                measure JSON for the PR
  state/run-input.json, state/tickets-index.json, state/learning/
```

The folder is git-ignored, and the pull request does not use local git: `drlPrSubmitter` reads the approved files from this folder and commits them to GitHub through the API. Set the `DRL_DATA_DIR` environment variable to use a different folder. Artifacts are kept until you delete them.

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

- Python with `uv` (`python -m uv --version`) and Node.js (for `npx`).
- Environment variables in the shell that launches VS Code: `ADO_PAT`, `GITHUB_TOKEN`, `API_AUTH_BASIC_TOKEN`; `LIBRARIAN_API_KEY` and `LIBRARIAN_API_SECRET` if used.
- Recommended: a local `rules-engine` checkout, with `RULES_ENGINE_REPO_PATH` pointing to it.
- An `ado-content` MCP server (used by `drlSourceIntake` to read Azure DevOps tickets). It is not bundled; add it once to your user MCP configuration (**MCP: Open User Configuration**), and VS Code starts it automatically:

```json
"ado-content": {
  "type": "stdio",
  "command": "npx",
  "args": ["-y", "@azure-devops/mcp@next", "healthcatalyst", "-a", "envvar",
           "-d", "core", "work", "work-items", "repositories", "search", "wiki", "pipelines", "advanced-security", "test-plans"],
  "env": { "ADO_MCP_AUTH_TOKEN": "${env:ADO_PAT}" }
}
```

The `local-python` server is bundled with the plugin and starts on its own. If a workspace already defines its own `local-python` or `ado-content` server, the names clash; use a workspace without them, such as `rules-engine`.

## Install

1. Add this repo as a marketplace in your VS Code user settings (`settings.json`), pointing at the branch that holds the plugin:

   ```json
   "chat.plugins.marketplaces": ["TriptiMandhyan/Agent-Plugin#measureable-agents-plugin"]
   ```

2. In the Extensions view, search `@agentPlugins` and install **drl-pipeline**. VS Code then checks for new versions about every 24 hours, so updates arrive on their own.
3. Add the `ado-content` server and set the environment variables (see Requirements), then restart VS Code.
4. Open the project you work in (for example `rules-engine`), select `drlFlowOrchestrator`, and give it a ticket ID or work-item URL.

You can also install without the marketplace through **Chat: Install Plugin From Source** with the repo URL, but the plugin must then be on the repo's default branch and updates are manual (`/plugin update`).

Nobody has to start a server by hand: VS Code launches `local-python` and `ado-content` when they are needed.

## Reviewing generated files

Before asking for approval, `drlFlowOrchestrator` calls the `list_artifacts` tool and tells you the exact folder, `drl-pipeline/artifacts/<ticket-id>/`. Review these before approving:

- `patches/*-drl.patch`: the DRL changes
- `mapping.md`, `plan.md`
- `drls/generated/`: the full generated DRLs

If you edit a generated DRL after approving, the PR step refuses to push (it checks the files against the approved patch), so make changes first or approve again.

## Known limitations

- The MCP server starts through `py -3 -m uv`, so it currently assumes Windows with the Python launcher.
- Some text under `runtime/agents/Drl Pipeline/` and in the agents still shows the original repo paths (`.github/agents/Drl Pipeline/...`). The MCP tools resolve them to the data folder.
- If the MCP server is started from a folder that isn't your project, `drl-pipeline/` is created there instead; the agent reports the full path.

## For maintainers: updating from source

```powershell
git clone https://healthcatalyst@dev.azure.com/healthcatalyst/MeasureAble%202.0/_git/measureable_content_agents
.\scripts\sync-from-source.ps1 -SourcePath .\measureable_content_agents
```

A warning means a source file that has an overlay version changed. Merge the change into `overlay/`, then run again with `-UpdateBaseline`. Bump `version` in `plugin.json` before publishing so users receive the update.
