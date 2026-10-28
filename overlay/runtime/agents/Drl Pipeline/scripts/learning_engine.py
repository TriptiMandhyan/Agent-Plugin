#!/usr/bin/env python3
"""Learning Engine: Passive aggregation of execution metrics, CQL patterns, and error patterns.

This module runs after each stage completes and aggregates learnings without interfering
with the main workflow. It's optional and non-blocking.

Safety guarantees:
- Never writes to artifacts or state used by main pipeline
- All writes go to state/learning/ only
- Failures are caught and logged, not propagated
- Can be safely disabled by orchestrator if needed
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from state_manager import resolve_data_dir

ROOT_DIR = resolve_data_dir()


class LearningEngine:
    """Aggregate execution metrics, CQL patterns, and errors for workflow optimization."""

    LEARNING_DIR = ROOT_DIR / "state" / "learning"
    METRICS_FILE = LEARNING_DIR / "execution-metrics.json"
    PATTERNS_FILE = LEARNING_DIR / "cql-patterns.json"
    ERRORS_FILE = LEARNING_DIR / "error-patterns.json"

    @classmethod
    def initialize(cls) -> None:
        """Create learning directory if missing."""
        cls.LEARNING_DIR.mkdir(parents=True, exist_ok=True)

    @classmethod
    def record_execution(cls, ticket_id: str, stage: str, elapsed_seconds: float, success: bool, details: str = "") -> None:
        """Record execution timing and outcome for a pipeline stage.

        Args:
            ticket_id: Ticket ID being processed
            stage: Pipeline stage name (e.g., 'intake', 'extraction', 'conversion')
            elapsed_seconds: Time taken for stage
            success: Whether stage succeeded
            details: Optional context (e.g., number of CQL files, errors encountered)
        """
        try:
            cls.initialize()
            metrics = cls._load_json(cls.METRICS_FILE, {})

            # Aggregate by stage
            if stage not in metrics:
                metrics[stage] = {
                    "executions": [],
                    "successCount": 0,
                    "failureCount": 0,
                    "avgTimeSeconds": 0.0,
                }

            metrics[stage]["executions"].append({
                "ticketId": ticket_id,
                "timestamp": datetime.utcnow().isoformat(),
                "elapsedSeconds": elapsed_seconds,
                "success": success,
                "details": details,
            })

            # Keep only last 100 executions per stage
            if len(metrics[stage]["executions"]) > 100:
                metrics[stage]["executions"] = metrics[stage]["executions"][-100:]

            # Update aggregate stats
            if success:
                metrics[stage]["successCount"] += 1
            else:
                metrics[stage]["failureCount"] += 1

            times = [e["elapsedSeconds"] for e in metrics[stage]["executions"]]
            metrics[stage]["avgTimeSeconds"] = sum(times) / len(times) if times else 0.0

            cls._write_json(cls.METRICS_FILE, metrics)
        except Exception as e:
            # Silent fail: don't propagate learning errors
            print(f"[Learning] Execution record failed: {e}", file=__import__('sys').stderr)

    @classmethod
    def record_cql_patterns(cls, ticket_id: str, cql_content: str) -> None:
        """Analyze CQL extraction and record observed patterns.

        Args:
            ticket_id: Ticket being processed
            cql_content: Raw CQL text from extraction artifacts
        """
        try:
            cls.initialize()
            patterns = cls._load_json(cls.PATTERNS_FILE, {})

            # Extract common CQL patterns
            rule_count = len(re.findall(r'\brule\b', cql_content, re.IGNORECASE))
            when_count = len(re.findall(r'\bwhen\b', cql_content, re.IGNORECASE))
            then_count = len(re.findall(r'\bthen\b', cql_content, re.IGNORECASE))
            library_refs = re.findall(r'library\s+(\S+)', cql_content, re.IGNORECASE)
            using_refs = re.findall(r'using\s+(\S+)', cql_content, re.IGNORECASE)
            include_refs = re.findall(r'include\s+(\S+)', cql_content, re.IGNORECASE)

            record = {
                "ticketId": ticket_id,
                "timestamp": datetime.utcnow().isoformat(),
                "ruleCount": rule_count,
                "whenCount": when_count,
                "thenCount": then_count,
                "libraries": list(set(library_refs)),
                "usings": list(set(using_refs)),
                "includes": list(set(include_refs)),
                "contentLength": len(cql_content),
            }

            if "observations" not in patterns:
                patterns["observations"] = []

            patterns["observations"].append(record)

            # Keep last 50 observations
            if len(patterns["observations"]) > 50:
                patterns["observations"] = patterns["observations"][-50:]

            # Aggregate common libraries/usings
            all_libraries = defaultdict(int)
            all_usings = defaultdict(int)
            for obs in patterns["observations"]:
                for lib in obs.get("libraries", []):
                    all_libraries[lib] += 1
                for using in obs.get("usings", []):
                    all_usings[using] += 1

            patterns["commonLibraries"] = dict(sorted(all_libraries.items(), key=lambda x: x[1], reverse=True)[:10])
            patterns["commonUsings"] = dict(sorted(all_usings.items(), key=lambda x: x[1], reverse=True)[:10])

            cls._write_json(cls.PATTERNS_FILE, patterns)
        except Exception as e:
            print(f"[Learning] CQL pattern record failed: {e}", file=__import__('sys').stderr)

    @classmethod
    def record_error(cls, ticket_id: str, stage: str, error_type: str, error_message: str) -> None:
        """Record error patterns for troubleshooting and optimization.

        Args:
            ticket_id: Ticket where error occurred
            stage: Pipeline stage where error occurred
            error_type: Category (e.g., 'validation', 'mcp-call', 'parsing')
            error_message: Error description
        """
        try:
            cls.initialize()
            errors = cls._load_json(cls.ERRORS_FILE, {})

            if stage not in errors:
                errors[stage] = {
                    "byType": defaultdict(list),
                    "totalCount": 0,
                }

            errors[stage]["byType"] = dict(errors[stage].get("byType", {}))  # Ensure dict
            if error_type not in errors[stage]["byType"]:
                errors[stage]["byType"][error_type] = []

            errors[stage]["byType"][error_type].append({
                "ticketId": ticket_id,
                "timestamp": datetime.utcnow().isoformat(),
                "message": error_message[:200],  # Truncate long messages
            })

            # Keep last 20 errors per type
            for error_list in errors[stage]["byType"].values():
                if len(error_list) > 20:
                    del error_list[:len(error_list) - 20]

            errors[stage]["totalCount"] = sum(len(v) for v in errors[stage]["byType"].values())

            cls._write_json(cls.ERRORS_FILE, dict(errors))
        except Exception as e:
            print(f"[Learning] Error record failed: {e}", file=__import__('sys').stderr)

    @classmethod
    def get_summary(cls) -> dict[str, Any]:
        """Get current learning summary for reporting.

        Returns:
            Dict with metrics, patterns, and error insights
        """
        try:
            cls.initialize()
            return {
                "generated": datetime.utcnow().isoformat(),
                "metrics": cls._load_json(cls.METRICS_FILE, {}),
                "patterns": cls._load_json(cls.PATTERNS_FILE, {}),
                "errors": cls._load_json(cls.ERRORS_FILE, {}),
            }
        except Exception:
            return {"error": "Could not load learning summary"}

    @staticmethod
    def _load_json(path: Path, default: Any = None) -> Any:
        """Load JSON with UTF-8 BOM tolerance."""
        try:
            if path.exists():
                content = path.read_text(encoding="utf-8-sig")
                return json.loads(content)
            return default if default is not None else {}
        except Exception:
            return default if default is not None else {}

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        """Write JSON with UTF-8 encoding."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


if __name__ == "__main__":
    # CLI for testing/debugging
    import sys

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "summary":
            print(json.dumps(LearningEngine.get_summary(), indent=2))
        elif cmd == "test-record":
            LearningEngine.record_execution("TEST-001", "intake", 12.5, True, "5 CQL files")
            LearningEngine.record_cql_patterns("TEST-001", "rule TestRule when exists then debug()")
            LearningEngine.record_error("TEST-001", "conversion", "validation", "Empty rule body")
            print("Test records created. Check state/learning/")
    else:
        print("Learning Engine CLI. Usage: python learning_engine.py [summary|test-record]")
