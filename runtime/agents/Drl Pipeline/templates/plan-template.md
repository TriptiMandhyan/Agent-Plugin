# Conversion Plan: <ticket-id>

## Context

- Ticket: <ticket-id>
- Source repo: <repo-url>
- Source branch: <branch>
- CQL files: <list>

## Proposed Approach

1. <step>
2. <step>
3. <step>

## Rule Mapping Strategy

- Eligibility logic:
- Exclusion logic:
- Temporal logic:
- Terminology bindings:

## Attribution and EMR Output Contract

- Subject type: <Encounter|Patient|Unresolved>
- Planned EMR API shape: <addEncounterEMR|addPatientEMR>
- Attribution cardinality requirement: <org only|org+group+provider|encounter-scoped only>
- If org+group+provider is required, list required loops/identifiers explicitly:
	- org key behavior: <empty org key or explicit org id behavior>
	- group identifiers source: <field name>
	- provider identifier source: <field name>
- Reference parity statement: <which reference DRL output shape is being matched and why>

## Risks and Unknowns

- <risk or unknown>

## Assumptions

- <assumption>

## Validation Plan

- DRL syntax check:
- Scenario checks:
- Regression notes:
