# MCP Operations Quick Card

## State Management (Always Use These)

```
mcp_local-python-_state_get_current_ticket()
  → {ok, ticketId}
  Use: Get current ticket at start of agent

mcp_local-python-_state_read_pipeline(ticket_id)
  → {ok, ticketId, pipeline: {stage, details, timestamp}}
  Use: Check what stage we're in before proceeding

mcp_local-python-_state_write_pipeline(stage, details="", ticket_id)
  → {ok, stage, path}
  Use: Mark stage complete after outputs written
  Valid stages: extraction-complete, baseline-template-ready, 
                conversion-complete, pr-ready, pr-submitted

mcp_local-python-_state_reset_ticket_baseline(ticket_id)
  → {ok, deleted, skipped, baselineState}
  Use: Explicit reset only. Call only from /drlTicketReset after the user requests a clean rerun/reset.

mcp_local-python-_state_read_approval(ticket_id)
  → {ok, approved, approvedBy, notes, timestamp}
  Use: Check if approval was recorded before API/test/PR stages

mcp_local-python-_state_write_approval(approved, approved_by="", notes="", ticket_id)
  → {ok, approved, path}
  Use: Record explicit approval (drlApprovalRecorder only)
```

## Content Operations (Stage-Specific)

```
mcp_local-python-_write_intake_json(intake, ticket_id)
  → {ok, path, cqlPathCount} or {ok:false, errors}
  Use: In drlSourceIntake to write intake.json (the only way intake is created)
  Prerequisite: work item + PR metadata fetched; PR rows need prId and sourceCommit

mcp_local-python-_download_and_extract_cql(ticket_id)
  → {ok, returnCode, stderr}
  Use: In drlSourceIntake to download all CQL fully AND write extraction.json in one call
  Prerequisite: write_intake_json returned ok=true

mcp_local-python-_write_intake_plan(content, ticket_id)
  → {ok, planPath, approvalPath}
  Use: In drlSourceIntake to write plan.md and reset approval-status.json to approved=false
  Prerequisite: extraction.json exists

mcp_local-python-_build_prior_year_measure_template(ticket_id, measure_slug="", source_path="", source_year="")
  → {ok, measureSlug, currentYear, sourceYear, templatePath, metadataPath, yearReplacementCount}
  Use: In drlBaselineBuilder to fetch and prepare baseline DRL
  Prerequisite: extraction.json ready (PR-linked) or intake.json with githubFetchRequired:true

mcp_local-python-_get_pr_changes(project_url, repository, pull_request_id)
  → {ok, cqlPaths, sourceCommit, mergeCommit, sourceBranch, iterationId, changeCount}
  Use: In drlSourceIntake to fetch PR metadata including commit details
  Prerequisite: Valid PR ID extracted from ticket
  Returns: Complete PR metadata needed for extraction stage

mcp_local-python-_generate_drl_patch(ticket_id, measure_slug)
  → {ok, patchContent, patchPath}
  Use: In drlConverter after baseline ready to write DRL patches

mcp_local-python-_write_mapping_md(content, ticket_id)
  → {ok, path, changedDrls} or {ok:false, errors}
  Use: In drlConverter after patches; requires every changed DRL named with a non-empty patch

mcp_local-python-_build_current_year_tests(ticket_id)
  → {ok, built, skipped} or {ok:false, missing}
  Use: In drlTestBuilder (only call); rolls prior-year tests into pr-assets/

mcp_local-python-_open_drl_pull_request(ticket_id)
  → {ok, prUrl, number, prMd}
  Use: In drlPrSubmitter right after publish_approved_pr_files; template body, writes pr.md

mcp_local-python-_validate_conversion_artifacts(ticket_id)
  → {ok, valid, errors, warnings}
  Use: In drlConverter to check DRL/mapping before marking complete
```

## Platform Operations (Rare, Built Into Agents)

```
mcp_local-python-_organize_ticket_artifacts(ticket_id, clean=false)
  → Creates directory structure for ticket artifacts
  Use: Automatic on first intake, rarely called direct
```

## Key Rules

1. **Always read state before writing** → `state_read_pipeline()` first
2. **Always write state after outputs** → `state_write_pipeline(stage, ticket_id)` last
3. **Never direct file edits** → Use MCP state tools only for state
4. **No implicit reset** → Source intake, stage agents, and normal orchestration never call reset
5. **One approval per run** → drlApprovalRecorder writes it once

## No External File References

All guidance is embedded in agent instructions. Do NOT reference:
- MCP_TOOLS_REFERENCE.md (use this card instead)
- MCP tools embedded in .md files
- Decision trees or flowcharts
- External configuration files during agent execution

Each agent must be self-contained with inline MCP context.
