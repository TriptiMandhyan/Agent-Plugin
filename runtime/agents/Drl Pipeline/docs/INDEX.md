# Documentation Index

This index lists the active workflow documentation for the current CQL to DRL pipeline.

## Core Docs

1. [architecture.md](architecture.md) - System architecture and stage responsibilities.
2. [runtime-flow.md](runtime-flow.md) - End-to-end execution order, gates, and troubleshooting (includes GitHub Actions tab invocation path).
3. [local-runbook.md](local-runbook.md) - Local setup, MCP discipline, and operator run steps.

## Workflow Rules

1. [../copilot-instructions.md](../copilot-instructions.md) - DRL Pipeline-specific guardrails and definition of done.
2. [../WORKFLOW.md](../WORKFLOW.md) - DRL Pipeline workflow documentation and file dependencies.
3. [../../../orchestration/config.json](../../../orchestration/config.json) - Runtime manifest, guardrails, and agent wiring.
4. [../../../orchestration/statuses.json](../../../orchestration/statuses.json) - Canonical pipeline stage values.
5. [../prompts/agent_protocol.md](../prompts/agent_protocol.md) - Shared stage protocol and completion gates.
6. [../../../skills/hedis-drl/SKILL.md](../../../skills/hedis-drl/SKILL.md) - Dynamic HEDIS conversion skill, including CE and max coverage.
7. [../../../skills/mips-drl/SKILL.md](../../../skills/mips-drl/SKILL.md) - Dynamic MIPS conversion skill.
8. [../../../skills/cqm-drl/SKILL.md](../../../skills/cqm-drl/SKILL.md) - Dynamic CQM/CMS/eCQM conversion skill.
9. [../../../skills/drl-review-rules/SKILL.md](../../../skills/drl-review-rules/SKILL.md) - Pointer to the rules-engine PR-review rules for all programs.

## GitHub Actions / Cloud-Agent Setup

- [../.github/workflows/copilot-setup-steps.yml](../.github/workflows/copilot-setup-steps.yml) - Copilot cloud-agent environment setup workflow (must be on default branch).
- [../.github/workflows/orchestrator-run.yml](../.github/workflows/orchestrator-run.yml) - Manual Actions-tab entry point for pipeline runs.
- [../.github/copilot/mcp.json](../.github/copilot/mcp.json) - Repo-scoped MCP server config for cloud-agent discovery.

## Current Stage Order

1. `/drlSourceIntake`
2. `/drlBaselineBuilder`
3. `/drlConverter`
4. `/drlApprovalRecorder`
5. `/drlApiBuilder`
6. `/drlTestBuilder`
7. `/drlPrSubmitter`

Approval is required immediately before PR submission.
