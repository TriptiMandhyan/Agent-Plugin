# PR Review Agent Plugin

Standalone package for an evidence-first GitHub pull request reviewer that validates changes against linked Azure DevOps work items.

## Package Contents

- `plugin.json` defines identity, permissions, integrations, and compatibility.
- `agent.md` contains the review workflow and safety boundaries.
- `integration-contract.md` defines the host capabilities required by the agent.
- `scripts/package.ps1` validates and builds the distributable artifact.

## Build

From this repository:

```powershell
.\scripts\package.ps1
```

The build creates:

```text
dist/com.ablehealth.pr-review-agent-1.0.0.agentplugin
dist/com.ablehealth.pr-review-agent-1.0.0.agentplugin.sha256
```

The `.agentplugin` file is a ZIP archive with a custom extension. The company marketplace can store this file and return it to a compatible plugin host.

## Authentication

The package never contains credentials. The plugin host must obtain secrets from its credential store and expose read-only connectors using:

- `GITHUB_TOKEN` with access to the repositories being reviewed.
- `AZURE_DEVOPS_PAT` with Work Items read access.

Do not put either value in `plugin.json`, source control, marketplace metadata, logs, or chat output.

## Host Responsibilities

The company plugin host must:

1. Validate the package checksum and publisher signature before installation.
2. Reject archive entries that escape the installation directory.
3. Read and validate `plugin.json` against the company plugin schema.
4. Show the requested permissions to the user before installation.
5. Map the declared integration capabilities to authenticated GitHub and Azure DevOps connectors.
6. Load `agent.md` as the agent's controlling instructions.
7. Keep the connectors read-only unless a future plugin version explicitly requests additional permissions.

Until the company host implements this contract, the package is buildable and distributable but cannot execute independently.