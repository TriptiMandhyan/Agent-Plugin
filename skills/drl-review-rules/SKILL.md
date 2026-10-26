---
name: drl-review-rules
description: "Use when converting or reviewing any DRL (MIPS, CMS/CQM, HEDIS) before a rules-engine PR. Points to the canonical rules-engine copilot-instructions.md that the GitHub PR-review agent applies, plus local facts the converter needs."
---

# DRL Review Rules

The rules a DRL PR is reviewed against live in one place: `copilot-instructions.md` at the rules-engine repo root (Shared-Rate checklist #1–#16, Cross-Measure #1–#6). The GitHub agent reads it after the PR is opened. Read it with `read_rules_engine_file("copilot-instructions.md")` before editing, and follow it over the program skill when they disagree. Do not copy its rules into this repo.

This file adds only what the reviewer file does not say:

- **Scope.** Apply the rules to calls, rules, and declares you add or change. Do not rewrite untouched baseline code; list pre-existing violations under "Assumptions" in mapping.md.
- **Org attribution.** New or changed `addPatientEMR` org calls pass `""`. `null` and `""` both hash to `NO_ATTRIBUTION` (`ResultUtils.calculatePatientMeasureStatusId`), so the change is behavior-neutral. MIPS 2025 used `null`; MIPS 2026 mostly uses `""`; CMS and HEDIS 2026 baselines still contain many `null` calls.
- **No `daysBetween`.** `DateHelper` has no `daysBetween` helper (verified 2026-10-09). Use a `when`-clause guard, a 0-day fallback, or `DateHelper.retrieveDatesFromRange`.
- **Baseline defects not to copy.** These exist in MIPS 2026 baselines: 29 rules combine `salience -10` with a status call (#7), and 74 lines still use singular `$encounter.groupExternalId`.
- **Validator.** `validate_drl_changes` reports `[rules-engine review <tag>]` warnings on new or changed code only. These warnings never fail validation. Fix each one or justify it in mapping.md. The tags are:
  - A1 org `null`
  - A2 `null` encounterable at group/provider level (#10)
  - A3 `isPresent` (#8)
  - A4 odd supplemental pairs (#14)
  - A5 invalid key/type (#15)
  - A6 `toString()` date (#13)
  - B1 salience (#7)
  - B2 unkeyed marker (#6)
  - C1 query parameter count (#9)
  - D4 raw date duration (#12)
