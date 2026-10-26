# Agent Protocol (Adapted)

## Intent

Define shared behavior for all slash agents in this repository.

## Protocol

1. Resolve `ticketId` from `.github/agents/Drl Pipeline/state/run-input.json`, then read `.github/agents/Drl Pipeline/artifacts/<ticket-id>/state/pipeline-status.json` before writing outputs.
2. Read stage input artifact from previous phase.
3. Write only stage-owned artifact files.
4. Update pipeline/approval state only through MCP state tools, not direct file edits.
5. Preserve ticket id, source refs, and source file paths.
6. If helper utilities are used, use them for fetch/IO only; keep policy decisions in agent instructions.
7. Use MCP tools exclusively for ADO metadata, content, and state operations:
   - `local-python.get_pr_changes(project_url, repository, pull_request_id)` for PR iteration changes and commit details
   - `local-python.download_and_extract_cql` for the extraction stage: downloads all PR-linked CQL fully and writes `extraction.json` in one deterministic call (agents never hand-write CQL or `extraction.json`)
   - baseline stage uses GitHub MCP file retrieval for baseline DRL content (current year first, then year-1/year-2 fallback). For PR-linked tickets, `local-python.build_prior_year_measure_template` writes baseline artifacts and required baseline CQL diff assets; for no-PR tickets, only the baseline DRL and baseline metadata are written.
   - conversion stage uses `local-python.generate_drl_patch(ticket_id, measure_slug)` to write canonical full unified baseline->generated DRL patches
   - `local-python.state_read_pipeline` and `local-python.state_read_approval` for state reads
   - `local-python.state_write_pipeline` and `local-python.state_write_approval` for state writes

## Guardrails

- Never proceed to PR submission when approval is false.
- Never treat approval from prior runs as valid for the current run; approval must be explicitly recorded in the current run by `/drlApprovalRecorder` with approver identity provided at command time.
- Treat `case for tests` as a non-default exception path available only when the current run explicitly requests it. Never infer or prioritize it automatically.
- For ADO source CQL retrieval, never use main branch when PR source refs/commits are available.
- For GitHub baseline DRL retrieval (rules-engine), use `githubRepo.baseBranch` as the ref (typically `main`) and do not reuse ADO PR source refs/commits.
- For test-source retrieval, use `local-python.read_rules_engine_file` (local working copy first, GitHub fallback) and consume `content` from tool output; do not depend on transient chat download file paths.
- Never write secrets to artifacts.
- Mark unresolved logic as assumptions or blockers.
- Do not let helper scripts update stage transitions on behalf of the agent.
- Do not use helper scripts for extraction parsing, conversion, or PR submission stages.
- Do not use ad-hoc shell or PowerShell parsing commands (for example rg/grep/Select-String pipelines) as the primary stage logic when required artifacts are already local; use agent-native artifact reads and deterministic parsing in stage instructions.
- Do not use direct ADO REST URL calls from agent instructions when MCP tools are available. MCP transport is required.
- **MCP tool `local-python.get_pr_changes()` is the required path for PR metadata resolution. Never fall back to direct Python script execution for PR changes.**
- **All pipeline/approval state transitions must use `local-python.state_write_*` tools. Direct writes to state JSON files are not allowed.**
- Conversion outputs must use reference-style fact modeling (platform domain facts and minimal marker/helper declares).

## Stage Completion Gates

- Intake can complete only if `.github/agents/Drl Pipeline/artifacts/<ticket-id>/intake.json` exists. For PR-linked tickets, `cqlPaths` must be non-empty. For no-PR tickets, `githubFetchRequired: true` must be set.
- Extraction can complete only for PR-linked tickets, and only if both `.github/agents/Drl Pipeline/artifacts/<ticket-id>/extraction.json` and `.github/agents/Drl Pipeline/artifacts/<ticket-id>/plan.md` exist and required raw CQL content was fetched for intake `cqlPaths`.
- Baseline template stage can complete if `.github/agents/Drl Pipeline/artifacts/<ticket-id>/drls/baseline/<measure-slug>.drl` and `.github/agents/Drl Pipeline/artifacts/<ticket-id>/baseline/<measure-slug>-baseline.json` exist. For PR-linked tickets, `.github/agents/Drl Pipeline/artifacts/<ticket-id>/cqls/baseline/<measure-slug>-source-cql-<year>.cql` and `.github/agents/Drl Pipeline/artifacts/<ticket-id>/patches/<measure-slug>-cql-diff.patch` must also exist.
- Conversion can complete only if conversion artifacts are written for the current ticket, baseline template artifacts were consumed, canonical DRL patch artifacts exist, and `local-python.validate_conversion_artifacts(ticket_id)` passes.
- Approval-recording stage can complete only if conversion is complete, approval is explicitly written as true for the current run, and pipeline stage is `pr-ready`.
- API artifact stage can complete only if pipeline stage is `pr-ready` and `.github/agents/Drl Pipeline/artifacts/<ticket-id>/pr-assets/<measure-slug>.json` exists. In an explicit `case for tests`, API lookup may remain a documented blocker instead of producing JSON.
- Test artifact stage can complete only if `.github/agents/Drl Pipeline/artifacts/<ticket-id>/pr-assets/Year<measureYear><measureFamilyPascal><measureNumber>Test.java` exists and either the JSON artifact exists or the current run is an explicit `case for tests` with missing librarian JSON documented.
- PR stage can complete only if approval is true for the current run and `.github/agents/Drl Pipeline/artifacts/<ticket-id>/pr.md` exists, then pipeline stage is set to `pr-submitted`. In an explicit `case for tests`, the PR artifact must document the missing librarian JSON blocker.
- PR stage can complete only if the PR artifact includes `ADO Work Item: <ticketUrl>` directly under the `Add link to Tracker Story` section from `templates/pr-template.md`.

