# Source Intake Prompt Module

- Read ticket URL or ticket ID from `.github/agents/Drl Pipeline/state/run-input.json`.
- Use `.github/orchestration/config.json -> adoDefaults.projectUrl` when only `ticketId` is present.
- Fetch ADO work item and PR metadata via MCP tools only.
- Preserve all PR metadata fields returned by `local-python.get_pr_changes` in `intake.json`.
- Write a fresh `intake.json` by calling `local-python.write_intake_json(intake, ticket_id)`; require `ok=true`. Printing JSON in chat does not create the file.
- Use `intake.cqlPaths` as the authoritative source list for extraction.
- Produce CQL and extraction in one deterministic step: call `local-python.download_and_extract_cql(ticket_id)` exactly once, after intake is written; require `ok=true` / return code `0`.
- You author only the intake object and the plan markdown. `write_intake_json` and `write_intake_plan` write the files; the raw CQL files under `cqls/extraction/` and `extraction.json` are produced solely by `download_and_extract_cql`.
- Never open, edit, create, hand-write, summarize, or abbreviate any CQL file or `extraction.json`. If you are about to type a `library` or `using` line, stop — that is always wrong.
- If the tool returns non-zero, read `extraction-fetched.json` (`unresolvedCqlPaths`, `invalidRawFiles`, `fetchErrors`), write a blocker, and stop. Do not pause to ask whether to retry and do not fabricate content.
- Write the plan (tagging relevant conversion skills by path, not embedding long examples) with `local-python.write_intake_plan(content, ticket_id)`; it also initializes `approval-status.json` with `approved=false`.
- If any tool fails and you cannot fix your own input, stop with `BLOCKED: <tool> — <error>`. Never write files via shell/Python or ask another agent to write them.
- Do not call `state_reset_ticket_baseline` or any reset helper. Source intake never resets ticket artifacts.
- The only final state call is `local-python.state_write_pipeline(stage="extraction-complete", details=..., ticket_id=...)`.
- Set pipeline stage to `extraction-complete` only after intake, the tool-produced CQL files, `extraction-fetched.json`, `extraction.json`, plan, and approval placeholder artifacts exist.
