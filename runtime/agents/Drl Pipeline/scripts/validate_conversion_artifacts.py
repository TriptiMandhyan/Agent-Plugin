#!/usr/bin/env python3
"""Validate generated conversion artifacts for naming and EMR guard contracts."""

import argparse
import difflib
import json
import pathlib
import re
import sys
from typing import List, Tuple

RULE_PATTERN = re.compile(
    r"^Year\d{4}\.(?:Mips\d{1,4}|Cms\d{1,4}|[A-Za-z0-9_]+)\.[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*(?:\.EMR(?:\.(?:Org|Group|Provider))?)?$"
)
TICKET_STYLE_RULE_PATTERN = re.compile(r"^\d{4,}_[a-z0-9_]+$")
MARKER_PATTERN = re.compile(r"^Year\d{4}(?:Mips\d{1,4}|Cms\d{1,4}|[A-Za-z0-9_]+)EMR(?:Org|Group|Provider)?$")
TICKET_STYLE_MARKER_PATTERN = re.compile(r"^T\d{4,}_.+")
FORBIDDEN_WRAPPER_DECLARE_PATTERN = re.compile(
    r"^(?:M\d+|Measure\d+|[A-Za-z0-9]+)(?:Context|Evidence|EmrOutput|OutputContext)$"
)
RULE_BLOCK_PATTERN = re.compile(
    r"^rule\s+[\"'](?P<name>[^\"']+)[\"'](?P<body>.*?)\nend",
    flags=re.DOTALL | re.MULTILINE,
)
LEGACY_EMR_API_PATTERN = re.compile(r"\baddExtendedMeasure(?:Results|Status)\s*\(")
LEGACY_GROUP_ID_PATTERN = re.compile(r"\.\s*groupExternalId\b|\bgetGroupExternalId\s*\(")


def load_extraction(ticket_id: str, root: pathlib.Path) -> dict:
    extraction_path = root / "artifacts" / f"{ticket_id}" / "extraction.json"
    if not extraction_path.exists():
        raise FileNotFoundError(f"Missing extraction artifact: {extraction_path}")
    with extraction_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_baseline_measure_slugs(ticket_id: str, root: pathlib.Path) -> List[str]:
    baseline_dir = root / "artifacts" / ticket_id / "baseline"
    if not baseline_dir.exists():
        return []

    slugs: List[str] = []
    for baseline_json in sorted(baseline_dir.glob("*-baseline.json")):
        try:
            with baseline_json.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            continue

        slug = str(payload.get("measureSlug", "")).strip()
        if slug:
            slugs.append(slug.lower())

    # Preserve order while de-duplicating.
    return list(dict.fromkeys(slugs))


def canonical_drl_name_from_cql_path(source_cql_path: str) -> str:
    stem = pathlib.Path(source_cql_path).stem
    stem_lower = stem.lower()
    source_path_lower = str(source_cql_path).replace("\\", "/").lower()
    family_match = re.search(r"/20\d{2}/([^/]+)/", source_path_lower)
    family = family_match.group(1).strip().lower() if family_match else ""

    if stem_lower.endswith("adm"):
        stem_lower = stem_lower[:-3]

    if family and stem_lower.startswith(family):
        return f"{stem_lower}.drl"

    if stem_lower.startswith("mips"):
        suffix = stem_lower[len("mips") :]
        if suffix.isdigit():
            return f"mips{suffix}.drl"
        return f"{stem_lower}.drl"

    if stem.isdigit():
        if family:
            return f"{family}{stem.zfill(3)}.drl"
        return f"mips{stem}.drl"

    # Mixed basenames like 005Rate1 should still emit canonical mips-prefixed outputs.
    if re.match(r"^\d+[A-Za-z0-9]+$", stem_lower):
        if family:
            numeric_prefix_match = re.match(r"^(\d+)(.*)$", stem_lower)
            if numeric_prefix_match:
                number = numeric_prefix_match.group(1).zfill(3)
                suffix = numeric_prefix_match.group(2)
                return f"{family}{number}{suffix}.drl"
        return f"mips{stem_lower}.drl"

    return pathlib.Path(stem_lower).with_suffix(".drl").name


def generated_drl_dir(root: pathlib.Path, ticket_id: str) -> pathlib.Path:
    return root / "artifacts" / ticket_id / "drls" / "generated"


def find_generated_drl(generated_dir: pathlib.Path, slug_or_name: str) -> pathlib.Path:
    """Generated DRL for a slug/file name, matched case-insensitively (generated files may use
    rules-engine casing such as sharedLTIDuringPeriod.drl). Falls back to the lowercase path."""
    wanted = pathlib.Path(str(slug_or_name or "").strip()).stem.lower() + ".drl"
    if generated_dir.is_dir():
        matches = sorted(p for p in generated_dir.glob("*") if p.is_file() and p.name.lower() == wanted)
        if matches:
            exact = [p for p in matches if p.name == wanted]
            return exact[0] if exact else matches[0]
    return generated_dir / wanted


def generated_drl_patch_label(root: pathlib.Path, generated_path: pathlib.Path) -> str:
    """Canonical lowercase-slug patch header path, independent of the generated file's casing."""
    return relative_posix(generated_path.parent / f"{generated_path.stem.lower()}.drl", root)


def expected_drl_paths(extraction: dict, root: pathlib.Path, ticket_id: str) -> List[pathlib.Path]:
    generated_dir = generated_drl_dir(root, ticket_id)
    baseline_slugs = load_baseline_measure_slugs(ticket_id, root)
    if baseline_slugs:
        return [find_generated_drl(generated_dir, slug) for slug in baseline_slugs]

    files = extraction.get("files", [])
    output_paths: List[pathlib.Path] = []
    for file_info in files:
        src_path = file_info.get("path", "")
        if not src_path.lower().endswith(".cql"):
            continue
        drl_name = canonical_drl_name_from_cql_path(src_path)
        output_paths.append(find_generated_drl(generated_dir, drl_name))
    return output_paths


def resolve_extraction_cql_path(extraction: dict, root: pathlib.Path, ticket_id: str, slug: str) -> pathlib.Path:
    extraction_dir = root / "artifacts" / ticket_id / "cqls" / "extraction"
    preferred = extraction_dir / f"{slug}.cql"
    if preferred.exists():
        return preferred

    for file_info in extraction.get("files", []):
        src_path = file_info.get("path", "")
        if not src_path.lower().endswith(".cql"):
            continue
        if pathlib.Path(canonical_drl_name_from_cql_path(src_path)).stem != slug:
            continue
        basename = pathlib.Path(src_path).name
        candidate = extraction_dir / basename
        if candidate.exists():
            return candidate

    return preferred


def legacy_duplicate_drl_paths(extraction: dict, root: pathlib.Path, ticket_id: str) -> List[pathlib.Path]:
    baseline_slugs = load_baseline_measure_slugs(ticket_id, root)
    allowed_names = {f"{slug}.drl" for slug in baseline_slugs}

    files = extraction.get("files", [])
    duplicates: List[pathlib.Path] = []
    for file_info in files:
        src_path = file_info.get("path", "")
        if not src_path.lower().endswith(".cql"):
            continue
        basename = pathlib.Path(src_path).name

        legacy_name = pathlib.Path(basename).with_suffix(".drl").name
        if legacy_name.lower() in allowed_names:
            continue

        if not baseline_slugs:
            canonical_name = canonical_drl_name_from_cql_path(src_path)
            if canonical_name == legacy_name:
                continue

        # Check both old flat conversion dir and new generated dir for alias files.
        for check_dir in [
            root / "artifacts" / "conversion",
            root / "artifacts" / ticket_id / "drls" / "generated",
        ]:
            legacy_path = find_generated_drl(check_dir, legacy_name)
            if legacy_path.exists():
                duplicates.append(legacy_path)

    return duplicates


def parse_declares(text: str) -> List[str]:
    return re.findall(r"^declare\s+([A-Za-z0-9_]+)", text, flags=re.MULTILINE)


def parse_rules(text: str) -> List[str]:
    # Rule names can be wrapped in either single or double quotes.
    return re.findall(r"^rule\s+[\"']([^\"']+)[\"']", text, flags=re.MULTILINE)


def parse_queries(text: str) -> List[str]:
    return re.findall(r"^\s*query\s+[\"']?([A-Za-z0-9_][^\s\"'(]*)", text, flags=re.MULTILINE)


def parse_rule_blocks(text: str) -> List[Tuple[str, str]]:
    return [(m.group("name"), m.group("body")) for m in RULE_BLOCK_PATTERN.finditer(text)]


def emr_rule_has_guard(text: str, rule_name: str) -> bool:
    block_match = re.search(
        rf"rule\s+[\"']{re.escape(rule_name)}[\"'](?P<body>.*?)\nend",
        text,
        flags=re.DOTALL,
    )
    if not block_match:
        return False
    body = block_match.group("body")
    # Pattern 1: WHEN guard (not exists / not Fact()) + THEN insert — classic fire-once
    # Accept both zero-arg and parameterized constructors/constraints
    has_guard_not_exists = re.search(r"not\s*\(\s*exists\s+[A-Za-z0-9_]+\s*\(", body)
    has_guard_not_fact = re.search(r"\n\s*not\s+[A-Za-z0-9_]+\s*\(", body)
    has_marker_insert = re.search(r"insert\s*\(\s*new\s+[A-Za-z0-9_]+\s*\(", body)
    classic_guard = bool((has_guard_not_exists or has_guard_not_fact) and has_marker_insert)
    # Pattern 2: groupExternalIds loop — the for-loop body contains insert(new ...) which serves
    # as the array-migration deduplication pattern (no WHEN guard needed)
    loop_with_insert = bool(
        re.search(r"for\s*\(\s*String\s+\$\w+\s*:\s*\$\w+\.groupExternalIds\s*\)", body)
        and has_marker_insert
    )
    return classic_guard or loop_with_insert


def has_placeholder_only_logic(text: str, rule_name: str) -> bool:
    block_match = re.search(
        rf"rule\s+[\"']{re.escape(rule_name)}[\"'](?P<body>.*?)\nend",
        text,
        flags=re.DOTALL,
    )
    if not block_match:
        return False

    body = block_match.group("body")
    normalized = re.sub(r"\s+", " ", body).strip().lower()
    if "eval(true)" in normalized:
        return True

    # Catch effectively empty rule skeletons with no predicates or actions.
    return normalized in {"when then", "when then ;"}


def has_nearby_cql_comment(text: str, rule_name: str, max_lines: int = 20) -> bool:
    """Return True only when a CQL define block (not just any comment) appears just above the rule."""
    marker = f"rule '{rule_name}'"
    idx = text.find(marker)
    if idx < 0:
        marker = f'rule "{rule_name}"'
        idx = text.find(marker)
    if idx < 0:
        return False

    lines = text[:idx].splitlines()
    window = "\n".join(lines[-max_lines:])
    # Require an actual CQL define keyword inside a block comment directly above the rule,
    # not just any /* ... */ comment.
    return bool(re.search(r'/\*[^*]*\bdefine\b', window, re.IGNORECASE))


def validate_drl(path: pathlib.Path) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []

    if not path.exists():
        errors.append(f"Missing expected DRL artifact: {path}")
        return errors, warnings

    text = path.read_text(encoding="utf-8")
    declares = parse_declares(text)
    rules = parse_rules(text)
    rule_blocks = parse_rule_blocks(text)

    if "import com.ablehealth.model.*" not in text:
        warnings.append(f"Missing reference import in {path}: import com.ablehealth.model.*")
    if "global PatientResults controlSet;" not in text:
        warnings.append(f"Missing reference global in {path}: global PatientResults controlSet;")
    if "EncounterDenominator(" not in text:
        warnings.append(f"No EncounterDenominator usage found in {path}; verify denominator handoff pattern")

    if not rules:
        queries = parse_queries(text)
        if queries:
            warnings.append(
                f"[INFO] Query-only shared DRL in {path}: no rules, {len(queries)} query block(s); "
                "rule checks skipped"
            )
        else:
            errors.append(f"No rules found in {path}")

    if LEGACY_EMR_API_PATTERN.search(text):
        errors.append(
            f"Legacy ExtendedMeasure API found in {path}; use addEncounterEMR/addPatientEMR EMRv2 calls"
        )

    # CE: maxCoverageDateByPayer must be a declared Drools global, never initialized locally.
    _LOCAL_MAP_INIT = re.compile(r"new\s+java\.util\.HashMap\s*<[^>]*>\s*\(\s*\).*maxCoverage", re.DOTALL)
    _LOCAL_MAX_STREAM = re.compile(r"maxCoverage\w*\s*=\s*[^(]*\.stream\(\)")
    _MAX_COV_GLOBAL = re.compile(r"^global\s+Map.*\bmaxCoverageDateByPayer\b", re.MULTILINE)
    _MAX_COV_USED = re.compile(r"\bmaxCoverageDateByPayer\b")
    if _LOCAL_MAP_INIT.search(text) or _LOCAL_MAX_STREAM.search(text):
        errors.append(
            f"maxCoverageDateByPayer locally initialized in {path} — must be a Drools global, "
            "not a local HashMap or stream derivation. Add 'global Map<String, LocalDate> maxCoverageDateByPayer;' "
            "and register the measure in MapperUseEnrollmentsRules.json."
        )
    elif _MAX_COV_USED.search(text) and not _MAX_COV_GLOBAL.search(text):
        errors.append(
            f"maxCoverageDateByPayer used but not declared as global in {path} — "
            "add 'global Map<String, LocalDate> maxCoverageDateByPayer;' at the file top."
        )

    # CE max-coverage signature / import / anti-pattern checks.
    _CQL_COMMENT_MAX_COV = re.compile(r"/\*[^*]*GetMaxCoverageEndDateForPayer", re.DOTALL)
    if _MAX_COV_USED.search(text) or _CQL_COMMENT_MAX_COV.search(text):
        ce_calls = re.findall(r"addContinuousEnrollment\s*\(([^;]*?)\)", text, re.DOTALL)
        if ce_calls and any("coverageEndDate" not in c for c in ce_calls):
            errors.append(
                f"Max-coverage measure in {path}: addContinuousEnrollment called without coverageEndDate — "
                "the per-payer max coverage date is dropped. Use the max-coverage overload with "
                "'..., payer, \"\", coverageEndDate, hasCE, optionId'."
            )
        if "getEarliestDate(" in text and not re.search(
            r"import\s+static\s+com\.ablehealth\.helper\.DateHelper\.getEarliestDate\s*;", text
        ):
            errors.append(
                f"getEarliestDate(...) used in {path} but missing "
                "'import static com.ablehealth.helper.DateHelper.getEarliestDate;'."
            )
        if re.search(r"\bLocalDate\b", text) and not re.search(
            r"import\s+java\.time\.LocalDate\s*;|import\s+java\.time\.\*\s*;", text
        ):
            errors.append(f"LocalDate used in {path} but missing 'import java.time.LocalDate;'.")
        if re.search(r"\bLocalDate\.now\s*\(", text):
            errors.append(
                f"LocalDate.now() drives a continuous-enrollment interval in {path} — result becomes "
                "run-date dependent. Use program/encounter dates as in the WCCA/URI reference DRLs."
            )
        if re.search(r"maxCoverageDateByPayer\s*!=\s*null", text) or re.search(r"catch\s*\(\s*Exception", text):
            errors.append(
                f"Defensive null-check / try-catch around maxCoverageDateByPayer in {path} is not used in "
                "reference DRLs — read the global directly."
            )
        has_explicit_max_guard = bool(
            re.search(r"if\s+[^\n]+\s+is\s+after\s+\"GetMaxCoverageEndDateForPayer\"", text)
        )
        if has_explicit_max_guard:
            if re.search(r"coverageEndDate\s*==\s*null\s*\?", text):
                errors.append(
                    f"CQL has an explicit 'is after GetMaxCoverageEndDateForPayer(P) return false' guard in {path}, "
                    "but the DRL uses a nullable fallback for coverageEndDate. Preserve the false branch as "
                    "coverageEndDate != null && !visitDate.isAfter(coverageEndDate) before isContinuous(...)."
                )
            if not re.search(r"coverageEndDate\s*!=\s*null[\s\S]{0,180}!\s*[^;\n]+\.isAfter\(\s*coverageEndDate\s*\)", text):
                errors.append(
                    f"CQL has an explicit max-coverage start-date guard in {path}, but the DRL does not show "
                    "the required short-circuit pattern: coverageEndDate != null && !<start>.isAfter(coverageEndDate)."
                )

    # CE option fan-out must include all concrete CQL option IDs. The converter previously
    # copied only EMR-bearing options and omitted exclusions such as denominator_exclusion_deceased.
    if "addContinuousEnrollment(" in text:
        cql_option_ids = set(re.findall(r"Option\s+ID:\s*([A-Za-z0-9_\-]+)", text))
        ce_option_arrays = re.findall(r"String\[\]\s+optionIds\s*=\s*new\s+String\[\]\s*\{([^}]*)\}", text)
        if cql_option_ids and ce_option_arrays:
            ce_option_ids = set()
            for option_array in ce_option_arrays:
                ce_option_ids.update(re.findall(r'"([A-Za-z0-9_\-]+)"', option_array))
            missing_ce_options = sorted(cql_option_ids - ce_option_ids)
            if missing_ce_options:
                errors.append(
                    f"Continuous enrollment optionIds in {path} omit concrete CQL option ID(s): "
                    f"{', '.join(missing_ce_options)}. Build CE fan-out from all /* Option ID: ... */ "
                    "entries, including exclusions that do not emit EMR records."
                )

    # CE: anchor-date list consistency — CQL {'medical'} anchor with {'medical' and 'pharmacy'} interval
    # requires a separate medical-only collect list.
    _CQL_ANCHOR_MED_ONLY = re.compile(
        r"/\*.*HasEnrollmentOnDateByBenefitTypes.*\{'medical'\}.*\*/",
        re.DOTALL,
    )
    _CQL_INTERVAL_MED_PHARM = re.compile(
        r"/\*.*HasContinuousEnrollmentByBenefitTypes.*'medical'\s+and\s+'pharmacy'.*\*/",
        re.DOTALL,
    )
    _MEDICAL_PHARM_COLLECT = re.compile(
        r"from\s+collect\s*\([^)]*hasMedicalCoverage\s*==\s*true[^)]*hasPharmacyCoverage\s*==\s*true"
    )
    _MEDICAL_ONLY_COLLECT = re.compile(
        r"from\s+collect\s*\([^)]*hasMedicalCoverage\s*==\s*true[^)]*\)"
    )
    if (
        _CQL_ANCHOR_MED_ONLY.search(text)
        and _CQL_INTERVAL_MED_PHARM.search(text)
        and _MEDICAL_PHARM_COLLECT.search(text)
        and not _MEDICAL_ONLY_COLLECT.search(text)
    ):
        errors.append(
            f"CE anchor-date mismatch in {path}: CQL anchor uses {{' medical'}} but only a combined "
            "medical+pharmacy collect list exists. Add a separate medical-only enrollment list "
            "(medicalEnrollmentList pattern as in SAA 2026)."
        )

    if LEGACY_GROUP_ID_PATTERN.search(text):
        warnings.append(
            f"Singular groupExternalId usage found in {path}; verify against 2026 reference pattern for this measure"
        )

    path_text = path.as_posix().lower()
    is_hedis = "/hedis/" in path_text or "package com.ablehealth.measure.hedis." in text

    for rule_name in rules:
        if TICKET_STYLE_RULE_PATTERN.match(rule_name):
            errors.append(f"Ticket-style rule name found in {path}: {rule_name}")
        if not RULE_PATTERN.match(rule_name):
            if is_hedis:
                warnings.append(f"[HEDIS Name bypass] Rule name does not match contract in {path}: {rule_name}")
            else:
                errors.append(f"Rule name does not match contract in {path}: {rule_name}")
        if has_placeholder_only_logic(text, rule_name):
            errors.append(f"Placeholder-only rule logic found in {path}: {rule_name}")
        if ".EMR" in rule_name and not emr_rule_has_guard(text, rule_name):
            if is_hedis:
                warnings.append(f"[HEDIS EMR bypass] EMR rule missing fire-once guard/marker insert in {path}: {rule_name}")
            else:
                errors.append(f"EMR rule missing fire-once guard/marker insert in {path}: {rule_name}")
        if ".EMR" in rule_name and not has_nearby_cql_comment(text, rule_name):
            warnings.append(f"Consider adding nearby CQL comment context in {path}: {rule_name}")

    for rule_name, body in rule_blocks:
        if ".EMR" not in rule_name:
            continue
        if "addPatientEMR(" not in body and "addEncounterEMR(" not in body:
            errors.append(
                f"EMR rule does not emit EMRv2 output in {path}: {rule_name}"
            )

    for declare_name in declares:
        if FORBIDDEN_WRAPPER_DECLARE_PATTERN.match(declare_name):
            errors.append(f"Synthetic wrapper declare found in {path}: {declare_name}")
        if "EMR" not in declare_name:
            continue
        if TICKET_STYLE_MARKER_PATTERN.match(declare_name):
            errors.append(f"Ticket-style marker declare found in {path}: {declare_name}")
        if not MARKER_PATTERN.match(declare_name):
            warnings.append(f"Marker declare does not match recommended format in {path}: {declare_name}")

    if "addPatientEMR" not in text and "addEncounterEMR" not in text and "EmrOutput" not in text:
        warnings.append(f"No EMRv2 emission call/payload found in {path}; confirm this measure should emit EMR")

    return errors, warnings


def expected_patch_path(root: pathlib.Path, ticket_id: str, slug: str) -> pathlib.Path:
    return root / "artifacts" / ticket_id / "patches" / f"{ticket_id}-{slug}-drl.patch"


def baseline_drl_path(root: pathlib.Path, ticket_id: str, slug: str) -> pathlib.Path:
    return root / "artifacts" / ticket_id / "drls" / "baseline" / f"{slug}.drl"


def relative_posix(path: pathlib.Path, root: pathlib.Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def split_call_args(call_text: str) -> List[str]:
    return [part.strip() for part in re.split(r',\s*(?![^()]*\))', call_text)]


def extract_cql_emr_definitions(cql_text: str) -> dict:
    definitions = {}
    define_pattern = re.compile(
        r'define\s+"(?P<name>[^"]+)"\s*:(?P<body>.*?)(?=\ndefine\s+"|\Z)',
        re.DOTALL | re.IGNORECASE,
    )
    for match in define_pattern.finditer(cql_text):
        body = match.group("body")
        option_match = re.search(r'option_id\s*=\s*["\']?([A-Za-z0-9_]+)["\']?', body)
        population_match = re.search(r'population\s*=\s*([A-Za-z ]+)', body)
        if not option_match or not population_match:
            continue
        option_id = option_match.group(1).strip()
        if option_id.lower() == "all":
            continue
        definitions[option_id] = {
            "defineName": match.group("name").strip(),
            "population": population_match.group(1).strip(),
            "mentionsDenominatorExclusion": '"Denominator Exclusion"' in body,
        }
    return definitions


def normalize_population_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def expected_population_enum(cql_population: str, status_value: str) -> str:
    population = normalize_population_name(cql_population)
    status = str(status_value or "").strip().upper()
    if population == "denominator":
        return "PopulationEnum.denominator"
    if population == "exclusion":
        return "PopulationEnum.denominator_exclusion"
    if population == "exception":
        return "PopulationEnum.denominator_exception"
    if population == "numerator":
        if status == "SUCCESS":
            return "PopulationEnum.numerator_performance_met"
        if status == "GAP":
            return "PopulationEnum.numerator_performance_not_met"
        if status == "EXCLUSION":
            return "PopulationEnum.numerator_exclusion"
        return "PopulationEnum.numerator"
    return ""


def extract_cql_option_ids(cql_text: str) -> set:
    """Extract option_ids from CQL ExtendedMeasureResultV2 returns."""
    option_ids = set()
    patterns = [
        r"option_id\s*=\s*([A-Za-z0-9_]+)",
        r"option_id\s*:\s*([A-Za-z0-9_]+)",
        r"option_id\s*=\s*['\"]([A-Za-z0-9_]+)['\"]"]
    for pattern in patterns:
        matches = re.findall(pattern, cql_text, re.IGNORECASE)
        for m in matches:
            val = m.strip()
            if val.lower() != "all":
                option_ids.add(val)
    return option_ids


def extract_drl_emr_option_ids(drl_text: str) -> set:
    """Extract option_ids from DRL addEncounterEMR/addPatientEMR calls."""
    option_ids = set()
    patterns = [
        r"addEncounterEMR\s*\([^)]*[,\s]+['\"]([A-Za-z0-9_]+)['\"]" ,
        r"addPatientEMR\s*\([^)]*[,\s]+['\"]([A-Za-z0-9_]+)['\"]"]
    for pattern in patterns:
        matches = re.findall(pattern, drl_text, re.IGNORECASE)
        option_ids.update(m.strip() for m in matches)
    return option_ids


def validate_status_emr_semantics(cql_text: str, drl_text: str, path: pathlib.Path) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []

    definitions = extract_cql_emr_definitions(cql_text)
    if not definitions:
        return errors, warnings

    definitions_by_lower = {option.lower(): (option, meta) for option, meta in definitions.items()}
    exclusion_guard_pattern = re.compile(r"not\s*\(\s*exists\s+[A-Za-z0-9_]*Exclusion[A-Za-z0-9_]*\s*\(")
    status_call_pattern = re.compile(
        r"add(?:Encounter|Patient|GroupPatient|ProviderPatient)MeasureStatus\s*\(.*?MeasureStatusValue\.(SUCCESS|GAP|EXCLUSION|EXCEPTION|DENOMINATOR).*?['\"]([A-Za-z0-9_]+)['\"]",
        re.DOTALL,
    )
    emr_call_pattern = re.compile(r"add(?:Encounter|Patient)EMR\s*\((?P<call>.*?)\)", re.DOTALL)

    for rule_name, body in parse_rule_blocks(drl_text):
        status_calls = []
        for status_value, option_id in status_call_pattern.findall(body):
            canonical = definitions_by_lower.get(option_id.lower())
            if not canonical:
                continue
            expected_option, metadata = canonical
            if option_id != expected_option:
                errors.append(
                    f"Option_id case mismatch in {path}: rule={rule_name}, expected={expected_option}, found={option_id}"
                )
            status_calls.append((status_value, expected_option, metadata))

        emr_calls = []
        for match in emr_call_pattern.finditer(body):
            params = split_call_args(match.group("call"))
            if len(params) < 7:
                continue
            option_param = params[5].strip().strip('"\'')
            population_param = params[6].strip()
            canonical = definitions_by_lower.get(option_param.lower())
            if not canonical:
                continue
            expected_option, metadata = canonical
            if option_param != expected_option:
                errors.append(
                    f"Option_id case mismatch in {path}: rule={rule_name}, expected={expected_option}, found={option_param}"
                )
            emr_calls.append((expected_option, population_param, metadata))

        emr_by_option = {option: population for option, population, _ in emr_calls}
        for status_value, option_id, metadata in status_calls:
            if (
                status_value != "EXCLUSION"
                and not metadata.get("mentionsDenominatorExclusion")
                and exclusion_guard_pattern.search(body)
            ):
                errors.append(
                    f"Rule introduces exclusion suppression not present in owning CQL definition in {path}: rule={rule_name}, option_id={option_id}"
                )
            if option_id not in emr_by_option:
                continue
            expected_population = expected_population_enum(metadata.get("population", ""), status_value)
            actual_population = emr_by_option[option_id]
            if expected_population and actual_population != expected_population:
                errors.append(
                    f"EMR population mismatch in {path}: rule={rule_name}, option_id={option_id}, expected={expected_population}, found={actual_population}"
                )

    return errors, warnings


def validate_emr_coverage(cql_text: str, drl_text: str, path: pathlib.Path) -> Tuple[List[str], List[str]]:
    """Verify every CQL ExtendedMeasureResultV2 has matching EMR rule."""
    errors = []
    warnings = []

    cql_option_ids = extract_cql_option_ids(cql_text)
    drl_emr_option_ids = extract_drl_emr_option_ids(drl_text)
    missing_emr = cql_option_ids - drl_emr_option_ids

    if missing_emr:
        errors.append(
            f"Missing EMR rules for option_ids in {path}: {', '.join(sorted(missing_emr))}. "
            f"Each CQL ExtendedMeasureResultV2 return requires a corresponding addEncounterEMR/addPatientEMR rule."
        )

    return errors, warnings


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def load_ticket_source_cql_text(root: pathlib.Path, ticket_id: str) -> str:
    """Concatenate every fetched/extracted CQL for the ticket (artifacts/<ticket>/cqls/**/*.cql)."""
    cql_root = root / "artifacts" / ticket_id / "cqls"
    if not cql_root.is_dir():
        return ""
    texts: List[str] = []
    for cql_file in sorted(cql_root.rglob("*.cql")):
        try:
            texts.append(cql_file.read_text(encoding="utf-8-sig", errors="replace"))
        except OSError:
            continue
    return "\n".join(texts)


_COMMENT_DELIMITER = re.compile(r"/\*+|\*+/")
_COMMENT_LINE_PREFIX = re.compile(r"^[ \t]*(?://+|\*+)", re.MULTILINE)


def _normalize_comment_text(text: str) -> str:
    """Strip comment markers ('/*', '*/', leading '*' or '//') and collapse all whitespace runs."""
    stripped = _COMMENT_DELIMITER.sub(" ", text or "")
    stripped = _COMMENT_LINE_PREFIX.sub(" ", stripped)
    return _normalize_ws(stripped)


def ellipsis_text_in_source_cql(text: str, normalized_source_cql: str) -> bool:
    """True when text appears verbatim in the ticket's source CQL.

    Comparison is whitespace-insensitive (tabs/newlines/space runs collapse to one space) and
    ignores comment markers on both sides; normalized_source_cql must come from
    _normalize_comment_text.
    """
    normalized = _normalize_comment_text(text)
    if not normalized or not normalized_source_cql:
        return False
    # A bare '...' carries no source evidence; require real text around the ellipsis.
    if not re.sub(r"[.\s]", "", normalized):
        return False
    return normalized in normalized_source_cql


def validate_cql_comment_completeness(
    drl_text: str, path: pathlib.Path, source_cql_text: str = ""
) -> Tuple[List[str], List[str]]:
    """Verify CQL comments are not summarized/truncated.

    A '...' is accepted only when the whole comment block appears verbatim (whitespace-insensitive,
    comment markers ignored) in the ticket's source CQL (source_cql_text); otherwise it is a summarization marker.
    """
    errors = []
    warnings = []
    normalized_source_cql = _normalize_comment_text(source_cql_text)

    # Strip CQL-style ellipsis from interval notation (e.g. Interval[..., end) uses ".."
    # not "..."). A three-dot ellipsis in a CQL comment is a truncation marker unless the
    # comment is copied verbatim from the source CQL.
    _TRUNCATION_ELLIPSIS = re.compile(r'(?<!\.)\.\.\.(?!\.)')

    cql_comment_pattern = re.compile(r"/\*\s*(.*?)\s*\*/", re.DOTALL)
    cql_blocks = cql_comment_pattern.findall(drl_text)

    for idx, block in enumerate(cql_blocks):
        block_lower = block.lower()

        if _TRUNCATION_ELLIPSIS.search(block):
            if ellipsis_text_in_source_cql(block, normalized_source_cql):
                warnings.append(
                    f"[INFO] CQL comment block {idx+1} in {path} contains '...' but matches the "
                    "ticket's source CQL verbatim; accepted."
                )
            else:
                errors.append(
                    f"CQL comment block {idx+1} in {path} contains '...' (summarization marker). "
                    f"CQL comments must be verbatim, not abbreviated."
                )

        if 'omitted' in block_lower:
            errors.append(
                f"CQL comment block {idx+1} in {path} contains 'omitted' keyword. "
                f"CQL comments must be complete, not summarized."
            )

        # Warn when a multi-define block appears to reference other definitions
        # (e.g. `union "Other Definition"`) without including them inline.
        if 'define ' in block_lower:
            has_union_intersect_ref = bool(
                re.search(r'\b(?:union|intersect|except)\s+"', block, re.IGNORECASE)
            )
            inline_define_count = block.count('define ')
            if has_union_intersect_ref and inline_define_count < 2:
                warnings.append(
                    f"CQL comment block {idx+1} in {path} references union/intersect/except "
                    f"definitions but may not include their full text. "
                    f"Verify all referenced definitions are included verbatim."
                )

    return errors, warnings


def validate_evidence_binding(drl_text: str, path: pathlib.Path) -> Tuple[List[str], List[str]]:
    """Verify EMR rules use bound evidence variables, not null/placeholders."""
    errors = []
    warnings = []

    emr_call_pattern = re.compile(
        r"(addEncounterEMR|addPatientEMR)\s*\([^)]*?\)",
        re.DOTALL
    )

    for match in emr_call_pattern.finditer(drl_text):
        emr_call = match.group(0)
        params = split_call_args(emr_call)

        if len(params) >= 8:
            evidence_param = params[7].strip() if len(params) > 7 else ""

            call_allows_null_evidence = bool(
                "SupplementalInfoEnum.CALCULATION_VALUE" in emr_call
                or "SupplementalInfoEnum.CALCULATED_VALUE_TYPE" in emr_call
            )
            if evidence_param in ['null', 'NULL', '']:
                if not call_allows_null_evidence:
                    errors.append(
                        f"EMR rule in {path} uses null evidence parameter. "
                        f"Evidence must be a bound variable unless the row is a calculated-value supplemental output."
                    )
            elif evidence_param.startswith('\"') or evidence_param.startswith("'"):
                errors.append(
                    f"EMR rule in {path} appears to use string literal as evidence parameter. "
                    f"Evidence must be a bound record variable."
                )
            elif not evidence_param.startswith('$'):
                warnings.append(
                    f"EMR rule in {path} may not be using properly bound evidence variable. "
                    f"Expected format: $variableName. Found: {evidence_param[:50]}"
                )

    return errors, warnings


def validate_program_context_patterns(drl_text: str, path: pathlib.Path) -> Tuple[List[str], List[str]]:
    """Validate program-specific context-id conventions observed in rules-engine 2026 DRLs."""
    errors: List[str] = []
    warnings: List[str] = []
    path_text = path.as_posix().lower()
    is_cms = "/cms/" in path_text or "package com.ablehealth.measure.cms." in drl_text
    is_mips = "/mips/" in path_text or "package com.ablehealth.measure.mips." in drl_text

    patient_emr_pattern = re.compile(r"addPatientEMR\s*\((?P<call>.*?)\)", re.DOTALL)
    for match in patient_emr_pattern.finditer(drl_text):
        params = split_call_args(match.group("call"))
        if len(params) < 4:
            continue
        context_param = params[3].strip()
        if is_cms and context_param == "null":
            errors.append(
                f"CMS/CQM org EMR contextId is null in {path}; rules-engine 2026 CMS DRLs use an empty string for org scope."
            )
        if is_mips and context_param == '""':
            warnings.append(
                f"MIPS org EMR contextId is an empty string in {path}; verify against parent 2026 MIPS DRLs, which commonly use null for org scope."
            )

    return errors, warnings


def validate_cql_comment_year_freshness(
    drl_text: str, path: pathlib.Path, baseline_metadata: dict
) -> Tuple[List[str], List[str]]:
    """Warn when CQL comment blocks appear to contain prior-year text that was not refreshed.

    If the baseline DRL was rolled from source_year to current_year, each converted rule's
    CQL comment block should reference current_year definitions.  When prior-year tokens
    dominate, it is likely the agent left the baseline-year CQL text in place.
    """
    errors: List[str] = []
    warnings: List[str] = []

    source_year = str(baseline_metadata.get("sourceYear") or "").strip()
    current_year = str(baseline_metadata.get("currentYear") or "").strip()

    if not source_year or not current_year or source_year == current_year:
        return errors, warnings

    cql_comment_pattern = re.compile(r"/\*\s*(.*?)\s*\*/", re.DOTALL)
    for idx, block in enumerate(cql_comment_pattern.findall(drl_text)):
        if "define " not in block.lower():
            continue
        prior_count = len(re.findall(rf"\b{re.escape(source_year)}\b", block))
        current_count = len(re.findall(rf"\b{re.escape(current_year)}\b", block))
        if prior_count > 0 and current_count == 0:
            warnings.append(
                f"CQL comment block {idx+1} in {path} still references {source_year} "
                f"with no {current_year} tokens; verify this comment was refreshed with "
                f"current-year CQL definitions."
            )

    return errors, warnings


def validate_complete_patch(
    root: pathlib.Path, ticket_id: str, slug: str, source_cql_text: str = ""
) -> List[str]:
    errors: List[str] = []
    normalized_source_cql = _normalize_comment_text(source_cql_text)

    baseline_path = baseline_drl_path(root, ticket_id, slug)
    generated_path = find_generated_drl(generated_drl_dir(root, ticket_id), slug)
    patch_path = expected_patch_path(root, ticket_id, slug)

    if not baseline_path.exists():
        errors.append(f"Missing baseline DRL for patch validation: {baseline_path}")
        return errors
    if not generated_path.exists():
        errors.append(f"Missing generated DRL for patch validation: {generated_path}")
        return errors
    if not patch_path.exists():
        errors.append(f"Missing DRL patch artifact: {patch_path}")
        return errors

    baseline_text = baseline_path.read_text(encoding="utf-8")
    generated_text = generated_path.read_text(encoding="utf-8")
    patch_text = patch_path.read_text(encoding="utf-8")

    expected_lines = list(
        difflib.unified_diff(
            baseline_text.splitlines(keepends=True),
            generated_text.splitlines(keepends=True),
            fromfile=relative_posix(baseline_path, root),
            tofile=generated_drl_patch_label(root, generated_path),
        )
    )
    expected_text = "".join(expected_lines)

    if patch_text != expected_text:
        errors.append(
            "DRL patch is not a complete canonical unified diff baseline->generated: "
            f"{patch_path}"
        )

    # Ignore removed baseline lines when looking for summarization markers.
    # Real unified diffs can legitimately contain "..." in deleted content.
    abbreviation_lines = []
    for line in patch_text.splitlines():
        if line.startswith("---") or line.startswith("+++") or line.startswith("@@"):
            continue
        if line.startswith("-"):
            continue
        # A '...' copied verbatim from the ticket's source CQL (e.g. a CQL comment) is not a summary.
        if "..." in line and ellipsis_text_in_source_cql(line[1:], normalized_source_cql):
            continue
        abbreviation_lines.append(line)

    abbreviation_text = "\n".join(abbreviation_lines).lower()
    if "..." in abbreviation_text or "omitted" in abbreviation_text or "summar" in abbreviation_text:
        errors.append(f"DRL patch appears abbreviated/summarized: {patch_path}")

    return errors


def option_ids_from_cql_diff_patch(root: pathlib.Path, ticket_id: str, slug: str) -> List[str]:
    patch_path = root / "artifacts" / ticket_id / "patches" / f"{slug}-cql-diff.patch"
    if not patch_path.exists():
        return []

    text = patch_path.read_text(encoding="utf-8", errors="replace")
    option_ids = re.findall(r"option_id\s*=\s*([a-zA-Z0-9_]+)", text)
    cleaned: List[str] = []
    for oid in option_ids:
        value = oid.strip()
        if not value or value.lower() == "all":
            continue
        cleaned.append(value)
    return list(dict.fromkeys(cleaned))


def validate_emrv2_option_parity(root: pathlib.Path, ticket_id: str, slug: str, drl_path: pathlib.Path) -> List[str]:
    errors: List[str] = []
    expected_options = option_ids_from_cql_diff_patch(root, ticket_id, slug)
    if not expected_options:
        return errors

    text = drl_path.read_text(encoding="utf-8", errors="replace")
    has_emrv2_emission = "addEncounterEMR(" in text or "addPatientEMR(" in text
    if not has_emrv2_emission:
        errors.append(
            f"CQL diff contains EMRv2 option_id entries but no EMRv2 emission call in {drl_path}"
        )
        return errors

    missing = [oid for oid in expected_options if f'\"{ oid}"' not in text]
    if missing:
        errors.append(
            "Missing EMRv2 option_id parity from CQL diff in generated DRL "
            f"{drl_path}: {', '.join(missing)}"
        )

    return errors


def validate_definition_level_emrv2_mapping(
    root: pathlib.Path,
    ticket_id: str,
    slug: str,
    drl_path: pathlib.Path,
) -> List[str]:
    """Ensure CQL option_id outputs map to rule-level EMRv2 for exclusion/exception status rules."""
    errors: List[str] = []
    expected_options = set(option_ids_from_cql_diff_patch(root, ticket_id, slug))
    if not expected_options:
        return errors

    text = drl_path.read_text(encoding="utf-8", errors="replace")
    rule_blocks = {name: body for name, body in parse_rule_blocks(text)}

    status_call_pattern = re.compile(
        r"addEncounterMeasureStatus\s*\(.*?MeasureStatusValue\.(EXCLUSION|EXCEPTION).*?\"([a-zA-Z0-9_]+)\"\s*\)",
        flags=re.DOTALL,
    )

    for rule_name, body in rule_blocks.items():
        has_emr = "addEncounterEMR(" in body or "addPatientEMR(" in body
        for status_value, option_id in status_call_pattern.findall(body):
            option = option_id.strip()
            if option not in expected_options:
                continue
            if status_value not in {"EXCLUSION", "EXCEPTION"}:
                continue
            if not has_emr or f'\"{ option}"' not in body:
                errors.append(
                    "Rule emits status for option_id from CQL but lacks matching rule-level EMRv2 output "
                    f"in {drl_path}: rule={rule_name}, status={status_value}, option_id={option}"
                )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate conversion DRL artifacts")
    parser.add_argument("--ticket", required=True, help="Ticket ID (for example 1048701)")
    parser.add_argument(
        "--root",
        default=".",
        help="Repository root path. Defaults to current working directory.",
    )
    args = parser.parse_args()

    ticket_id = args.ticket.strip()
    root_path = pathlib.Path(args.root).resolve()

    try:
        extraction = load_extraction(ticket_id, root_path)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 2

    expected_drls = expected_drl_paths(extraction, root_path, ticket_id)
    if not expected_drls:
        print("ERROR: No changed CQL files found in extraction artifact")
        return 2

    duplicate_legacy_paths = legacy_duplicate_drl_paths(extraction, root_path, ticket_id)

    errors: List[str] = []
    warnings: List[str] = []
    source_cql_text = load_ticket_source_cql_text(root_path, ticket_id)

    for expected_drl in expected_drls:
        if not expected_drl.exists():
            errors.append(f"Expected DRL not found: {expected_drl}")
            continue

        slug = expected_drl.stem.lower()
        cql_path = resolve_extraction_cql_path(extraction, root_path, ticket_id, slug)
        baseline_meta_path = root_path / "artifacts" / ticket_id / "baseline" / f"{slug}-baseline.json"

        drl_text = expected_drl.read_text(encoding="utf-8", errors="replace")
        cql_text = ""
        if cql_path.exists():
            cql_text = cql_path.read_text(encoding="utf-8", errors="replace")
        baseline_meta: dict = {}
        if baseline_meta_path.exists():
            try:
                with baseline_meta_path.open(encoding="utf-8") as _fh:
                    baseline_meta = json.load(_fh)
            except Exception:
                baseline_meta = {}

        drl_errors, drl_warnings = validate_drl(expected_drl)
        errors.extend(drl_errors)
        warnings.extend(drl_warnings)

        if cql_text:
            emr_errors, emr_warnings = validate_emr_coverage(cql_text, drl_text, expected_drl)
            errors.extend(emr_errors)
            warnings.extend(emr_warnings)

            semantic_errors, semantic_warnings = validate_status_emr_semantics(cql_text, drl_text, expected_drl)
            errors.extend(semantic_errors)
            warnings.extend(semantic_warnings)

        comment_errors, comment_warnings = validate_cql_comment_completeness(
            drl_text, expected_drl, source_cql_text
        )
        errors.extend(comment_errors)
        warnings.extend(comment_warnings)

        freshness_errors, freshness_warnings = validate_cql_comment_year_freshness(
            drl_text, expected_drl, baseline_meta
        )
        errors.extend(freshness_errors)
        warnings.extend(freshness_warnings)

        evidence_errors, evidence_warnings = validate_evidence_binding(drl_text, expected_drl)
        errors.extend(evidence_errors)
        warnings.extend(evidence_warnings)

        context_errors, context_warnings = validate_program_context_patterns(drl_text, expected_drl)
        errors.extend(context_errors)
        warnings.extend(context_warnings)

        errors.extend(validate_complete_patch(root_path, ticket_id, slug, source_cql_text))
        errors.extend(validate_emrv2_option_parity(root_path, ticket_id, slug, expected_drl))
        errors.extend(validate_definition_level_emrv2_mapping(root_path, ticket_id, slug, expected_drl))

    for warning_msg in warnings:
        print(f"WARNING: {warning_msg}")

    if errors:
        for error_msg in errors:
            print(f"ERROR: {error_msg}")

    if duplicate_legacy_paths:
        for legacy_path in duplicate_legacy_paths:
            print(
                "ERROR: Legacy/basename duplicate DRL artifact found alongside canonical measure-slug output: "
                f"{legacy_path}"
            )
        errors.append("duplicate_legacy_drl")

    try:
        out_f = root_path / "artifacts" / ticket_id / "validation_results.txt"
        out_f.parent.mkdir(parents=True, exist_ok=True)
        with out_f.open("w", encoding="utf-8") as out_h:
            out_h.write("=== RUN COMPLETE ===\n")
            for w in warnings:
                out_h.write(f"WARNING: {w}\n")
            for e in errors:
                out_h.write(f"ERROR: {e}\n")
    except Exception as exc:
        print(f"FAILED TO WRITE CUSTOM RESULTS: {exc}")

    if errors:
        return 1

    print(f"Validation passed for ticket {ticket_id}: {len(expected_drls)} DRL artifact(s) checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
