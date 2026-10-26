#!/usr/bin/env python3
"""Local FastAPI-hosted MCP server for Python helper operations.

This exposes existing repository helper scripts as MCP tools so agents can
invoke them through MCP rather than direct shell commands.
"""

from __future__ import annotations

import base64
import copy
import difflib
import json
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from state_manager import PipelineStage, TicketStateManager
from learning_engine import LearningEngine


BUNDLE_DIR = Path(__file__).resolve().parent.parent
# Read-only plugin files live in BUNDLE_DIR; mutable state and artifacts go to DRL_DATA_DIR (PLUGIN_DATA).
ROOT_DIR = Path(os.environ.get("DRL_DATA_DIR") or BUNDLE_DIR).resolve()
ORCHESTRATION_CONFIG_PATH = Path(
    os.environ.get("ORCHESTRATION_CONFIG_PATH")
    or BUNDLE_DIR.parent.parent / "orchestration" / "config.json"
)
APP_HOST = os.environ.get("LOCAL_MCP_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("LOCAL_MCP_PORT", "8766"))

mcp = FastMCP("local-python-tools")
# Create the streamable app once to initialize session manager lazily.
_mcp_asgi_app = mcp.streamable_http_app()


# --- Retry Configuration ---
# Load retry config from orchestration config.json
_RETRY_CONFIG: dict[str, Any] = {}
try:
    if ORCHESTRATION_CONFIG_PATH.exists():
        with open(ORCHESTRATION_CONFIG_PATH, encoding="utf-8") as f:
            full_config = json.load(f)
            _RETRY_CONFIG = full_config.get("retryConfig", {})
except Exception:
    pass  # Fallback to empty config if file missing or malformed

# Defaults when retry config not found
_RETRY_ENABLED = _RETRY_CONFIG.get("enabled", True)
_MAX_RETRIES = _RETRY_CONFIG.get("maxRetries", 3)
_BACKOFF_STRATEGY = _RETRY_CONFIG.get("backoffStrategy", "exponential")
_INITIAL_DELAY_MS = _RETRY_CONFIG.get("initialDelayMs", 1000)
_MAX_DELAY_MS = _RETRY_CONFIG.get("maxDelayMs", 30000)
_BACKOFF_MULTIPLIER = _RETRY_CONFIG.get("backoffMultiplier", 2.0)
_JITTER_ENABLED = _RETRY_CONFIG.get("jitterEnabled", True)
_RETRYABLE_HTTP_CODES = set(_RETRY_CONFIG.get("retryableHttpCodes", [408, 429, 500, 502, 503, 504]))
_NON_RETRYABLE_HTTP_CODES = set(_RETRY_CONFIG.get("nonRetryableHttpCodes", [400, 401, 403, 404, 422]))
_RETRYABLE_ERROR_KEYWORDS = _RETRY_CONFIG.get("retryableErrors", ["timeout", "connection_reset", "connection_refused"])


def _calculate_backoff_delay(attempt: int) -> float:
    """Calculate backoff delay in seconds based on attempt number."""
    if not _RETRY_ENABLED or _BACKOFF_STRATEGY == "none":
        return 0.0
    
    if _BACKOFF_STRATEGY == "linear":
        delay_ms = _INITIAL_DELAY_MS * attempt
    else:  # exponential (default)
        delay_ms = _INITIAL_DELAY_MS * (_BACKOFF_MULTIPLIER ** (attempt - 1))
    
    # Cap at max delay
    delay_ms = min(delay_ms, _MAX_DELAY_MS)
    
    # Add jitter to prevent thundering herd
    if _JITTER_ENABLED:
        jitter_range = delay_ms * 0.1  # ±10% jitter
        delay_ms += random.uniform(-jitter_range, jitter_range)
    
    return max(0, delay_ms / 1000.0)  # Convert to seconds


def _is_retryable_error(error: Exception) -> bool:
    """Check if an error is retryable based on configuration."""
    if not _RETRY_ENABLED:
        return False
    
    # HTTP error codes
    if isinstance(error, urllib.error.HTTPError):
        http_code = error.code
        if http_code in _NON_RETRYABLE_HTTP_CODES:
            return False
        if http_code in _RETRYABLE_HTTP_CODES:
            return True
    
    # URL errors (timeout, connection issues)
    if isinstance(error, urllib.error.URLError):
        error_str = str(error.reason).lower()
        for keyword in _RETRYABLE_ERROR_KEYWORDS:
            if keyword in error_str:
                return True
    
    # Timeout errors
    if isinstance(error, TimeoutError) or "timeout" in str(error).lower():
        return True
    
    return False


def _with_retry(
    func: Callable[[], Any],
    operation_name: str = "operation",
    max_retries: int | None = None,
) -> Any:
    """Execute a function with retry logic based on configuration.
    
    Args:
        func: Function to execute (should be a no-arg callable)
        operation_name: Human-readable name for logging
        max_retries: Override max retries (uses config default if None)
    
    Returns:
        Result from successful function call
    
    Raises:
        Last exception if all retries exhausted
    """
    if not _RETRY_ENABLED:
        return func()
    
    retries = max_retries if max_retries is not None else _MAX_RETRIES
    last_error: Exception | None = None
    
    for attempt in range(1, retries + 2):  # +1 for initial attempt, +1 for range exclusive end
        try:
            return func()
        except Exception as e:
            last_error = e
            
            # Don't retry on last attempt
            if attempt > retries:
                break
            
            # Check if error is retryable
            if not _is_retryable_error(e):
                raise
            
            # Calculate and apply backoff
            delay = _calculate_backoff_delay(attempt)
            print(
                f"[RETRY] {operation_name} failed (attempt {attempt}/{retries}): {type(e).__name__}: {e}. "
                f"Retrying in {delay:.2f}s...",
                file=sys.stderr,
            )
            time.sleep(delay)
    
    # All retries exhausted
    raise last_error  # type: ignore[misc]


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _run_python_script(args: list[str]) -> dict[str, Any]:
    """Run a repository helper script and return command result payload."""
    if args and not Path(args[0]).is_absolute():
        args = [str(BUNDLE_DIR / args[0]), *args[1:]]
    command = [sys.executable, *args]
    proc = subprocess.run(
        command,
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return {
        "ok": proc.returncode == 0,
        "returnCode": proc.returncode,
        "command": " ".join(command),
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def _resolve_ticket_id(ticket_id: str = "") -> str:
    """Resolve ticket ID from argument or run-input context."""
    ticket = str(ticket_id or "").strip()
    if not ticket:
        return TicketStateManager.get_current_ticket_id()
    # Ticket ids become artifact folder names, so only ADO numeric ids are allowed.
    if not ticket.isdigit() or len(ticket) > 20:
        raise ValueError(f"Invalid ticket id: {ticket!r}")
    return ticket


_SAFE_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


def _is_safe_slug(slug: str) -> bool:
    return bool(_SAFE_SLUG_RE.fullmatch(slug))


def _require_ado_url(url: str) -> str:
    """Reject URLs that would send ADO_PAT anywhere but Azure DevOps over https."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or not (
        host == "dev.azure.com" or host.endswith(".visualstudio.com")
    ):
        raise ValueError(f"project_url must be an https dev.azure.com or *.visualstudio.com URL: {url}")
    return url


def _safe_json_load(path: Path) -> Any:
    """Load JSON content when possible, otherwise return None."""
    try:
        if not path.exists() or path.is_dir():
            return None
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _copy_file(src: Path, dst: Path) -> bool:
    """Copy one file while ensuring destination parent directory exists."""
    if not src.exists() or src.is_dir():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = str(value or "").strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _ticket_measure_slugs(ticket_id: str) -> list[str]:
    """Resolve measure slugs for a ticket using baseline metadata first, generated DRLs second."""
    slugs: list[str] = []

    baseline_dir = ROOT_DIR / "artifacts" / ticket_id / "baseline"
    if baseline_dir.exists():
        for meta_path in sorted(baseline_dir.glob("*-baseline.json")):
            payload = _safe_json_load(meta_path)
            if isinstance(payload, dict):
                slug = str(payload.get("measureSlug") or "").strip()
                if slug:
                    slugs.append(slug)

    # Fall back to scanning generated DRL directory.
    generated_drls_dir = ROOT_DIR / "artifacts" / ticket_id / "drls" / "generated"
    if generated_drls_dir.exists():
        for drl_file in sorted(generated_drls_dir.glob("*.drl")):
            slugs.append(_generated_drl_slug(drl_file))

    return _unique(slugs)


# Generated DRLs may be named in rules-engine case (e.g. sharedLTIDuringPeriod.drl) while slugs,
# baselines, baseline metadata, and patches stay lowercase. The server runs on a case-sensitive
# Linux bind mount, so resolve generated files by case-insensitive stem match.
def _generated_drl_slug(path: Path) -> str:
    """Lowercase slug for a generated DRL file, independent of its on-disk casing."""
    return Path(path).stem.lower()


def _find_generated_drl(generated_dir: Path, slug: str) -> Path | None:
    """Existing generated DRL for a slug (case-insensitive), preferring an exact-case match."""
    if not generated_dir.is_dir():
        return None
    wanted = f"{str(slug or '').strip().lower()}.drl"
    matches = sorted(p for p in generated_dir.glob("*") if p.is_file() and p.name.lower() == wanted)
    if not matches:
        return None
    exact = [p for p in matches if p.name == wanted]
    return exact[0] if exact else matches[0]


def _generated_drl_path(generated_dir: Path, slug: str) -> Path:
    """Existing generated DRL for a slug (any casing), else the default lowercase `<slug>.drl` path."""
    found = _find_generated_drl(generated_dir, slug)
    return found if found is not None else generated_dir / f"{str(slug or '').strip().lower()}.drl"


def _generated_drl_label(root: Path, generated_path: Path) -> str:
    """Canonical (lowercase-slug) patch header path so renaming a generated DRL never changes its patch."""
    rel = Path(generated_path).relative_to(root)
    return str(rel.parent / f"{_generated_drl_slug(generated_path)}.drl").replace("\\", "/")


def _safe_remove_path(path: Path) -> bool:
    """Remove a file or directory if it exists."""
    if not path.exists():
        return False

    def _make_writable(target: str | Path) -> None:
        try:
            os.chmod(target, stat.S_IWRITE)
        except Exception:
            pass

    def _on_rm_error(func: Any, target: str, _exc_info: Any) -> None:
        _make_writable(target)
        func(target)

    # OneDrive-backed paths on Windows can transiently deny delete operations.
    # Retry with writable attributes before marking the path as not removable.
    for attempt in range(3):
        try:
            _make_writable(path)

            if path.is_dir():
                # Empty directory fast-path can avoid rmtree permission edge cases.
                try:
                    path.rmdir()
                    return True
                except OSError:
                    shutil.rmtree(path, onerror=_on_rm_error)
            else:
                path.unlink()

            return True
        except FileNotFoundError:
            return False
        except (PermissionError, OSError):
            if attempt == 2:
                break
            time.sleep(0.2 * (attempt + 1))

    return not path.exists()


def _format_template(value: str, context: dict[str, Any]) -> str:
    """Format a string template while preserving unknown placeholders."""
    return str(value).format_map(_SafeFormatDict({k: str(v) for k, v in context.items()}))


_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env_vars(value: Any) -> Any:
    """Expand ${ENV_VAR} placeholders recursively in strings/dicts/lists."""
    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand_env_vars(item) for item in value]
    if isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    return value


def _json_path_get(payload: Any, path: str) -> Any:
    """Read a simple dot-path from a JSON-like payload."""
    current = payload
    for part in str(path or "").split("."):
        key = part.strip()
        if not key:
            continue
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _json_path_set(payload: Any, path: str, value: Any) -> bool:
    """Set a simple dot-path on a JSON-like dict payload."""
    if not isinstance(payload, dict):
        return False

    parts = [part.strip() for part in str(path or "").split(".") if part.strip()]
    if not parts:
        return False

    current: Any = payload
    for key in parts[:-1]:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]

    final_key = parts[-1]
    if not isinstance(current, dict) or final_key not in current:
        return False

    current[final_key] = value
    return True


def _http_json_request(
    *,
    url: str,
    method: str,
    headers: dict[str, str] | None,
    body: Any,
    timeout_seconds: int,
) -> Any:
    """Execute an HTTP request with retry logic and parse the JSON response."""
    
    def _make_request() -> Any:
        request_data: bytes | None = None
        if body is not None:
            content_type = str((headers or {}).get("Content-Type", "")).lower()
            if "application/x-www-form-urlencoded" in content_type and isinstance(body, dict):
                request_data = urllib.parse.urlencode(body).encode("utf-8")
            else:
                request_data = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(url=url, data=request_data, method=str(method or "GET").upper())
        
        # Set Content-Length explicitly for POST with no body to match Postman behavior
        if request_data is None and method.upper() == "POST":
            req.add_header("Content-Length", "0")
        
        for key, val in (headers or {}).items():
            req.add_header(str(key), str(val))

        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8")

        if not raw.strip():
            raise ValueError(f"Empty response body from {url}")

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Non-JSON response from {url}: {exc}") from exc
    
    # Wrap the request in retry logic
    return _with_retry(
        func=_make_request,
        operation_name=f"HTTP {method} {url}",
    )


def _normalize_auth_headers(headers: dict[str, Any] | None) -> dict[str, str]:
    """Normalize outgoing auth headers to avoid malformed Authorization values."""
    normalized: dict[str, str] = {
        str(k): str(v) for k, v in (headers or {}).items()
    }

    auth_value = normalized.get("Authorization")
    if auth_value:
        trimmed = str(auth_value).strip()
        lower = trimmed.lower()
        if lower.startswith("basic basic "):
            normalized["Authorization"] = "Basic " + trimmed[12:].strip()

    return normalized


def _redacted_headers(headers: dict[str, Any] | None) -> dict[str, str]:
    """Return a copy of headers with sensitive values redacted."""
    out = {str(k): str(v) for k, v in (headers or {}).items()}
    for key in list(out.keys()):
        if key.lower() in {"authorization", "x-api-key", "api-key"}:
            original = str(out[key])
            # Always show length and first/last 4 chars for debugging without exposing full value
            if len(original) > 8:
                out[key] = f"<redacted len={len(original)} start={original[:4]}... end=...{original[-4:]}>"
            elif len(original) > 0:
                out[key] = f"<redacted len={len(original)}>"
            else:
                out[key] = "<redacted len=0 EMPTY>"
    return out


def _parse_last_modified_timestamp(value: Any) -> datetime | None:
    """Parse API last_modified timestamp into a comparable datetime."""
    if value is None:
        return None

    raw = str(value).strip()
    if not raw:
        return None

    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue

    return None


def _load_baseline_measure_slugs(ticket_id: str) -> list[str]:
    """Read canonical measure slugs from baseline metadata for a ticket."""
    baseline_dir = ROOT_DIR / "artifacts" / ticket_id / "baseline"
    if not baseline_dir.exists() or not baseline_dir.is_dir():
        return []

    slugs: list[str] = []
    for metadata_file in sorted(baseline_dir.glob("*-baseline.json")):
        try:
            payload = json.loads(metadata_file.read_text(encoding="utf-8-sig"))
        except Exception:
            continue

        slug = str(payload.get("measureSlug") or "").strip().lower()
        if slug:
            slugs.append(slug)

    return list(dict.fromkeys(slugs))


def _sync_ticket_artifact_bundle(ticket_id: str) -> dict[str, Any]:
    """Ensure canonical ticket-first directories exist."""
    bundle_root = ROOT_DIR / "artifacts" / ticket_id
    dirs = [
        bundle_root / "cqls" / "baseline",
        bundle_root / "cqls" / "extraction",
        bundle_root / "drls" / "baseline",
        bundle_root / "drls" / "generated",
        bundle_root / "patches",
        bundle_root / "baseline",
        bundle_root / "pr-assets",
        bundle_root / "state",
    ]
    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)

    return {
        "path": str(bundle_root.relative_to(ROOT_DIR)).replace("\\", "/"),
        "counts": {"patches": 0, "cqls": 0, "drls": 0},
        "sync": "canonical-only",
    }


def _load_json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _canonical_measure_slug_from_cql_path(source_cql_path: str, measure_family_lower: str) -> str:
    """Derive canonical measure slug from a CQL path/basename.

    The slug is the lowercase CQL filename stem, with optional transformations:
    - Strips trailing "ADM" suffix (e.g., cms138rate1ADM.cql -> cms138rate1)
    - Prefixes numeric-only stems with family name (e.g., 050.cql + mips -> mips050)
    - Leaves HEDIS-style names unchanged (e.g., TRCA.cql -> trca, TRCB.cql -> trcb)

    IMPORTANT — for HEDIS measures the family token ("hedis") does NOT appear in the
    CQL filename.  TRCA.cql belongs to family "hedis" but its slug is "trca", not
    "hedistrc" or "trc".  The function returns the raw stem in that case, which is
    exactly correct.  Never substitute intake.measureSlug ("trc") for individual
    measure slugs ("trca", "trcb", "trce", "trce7", "trcm").

    Examples:
        cms138rate1ADM.cql  + cms   -> cms138rate1
        cms138sharedADM.cql + cms   -> cms138shared
        138Rate2ADM.cql     + cms   -> cms138rate2   (family-prefixed)
        050.cql             + mips  -> mips050        (family-prefixed)
        TRCA.cql            + hedis -> trca           (raw stem — no family prefix in filename)
        TRCB.cql            + hedis -> trcb
        TRCE7.cql           + hedis -> trce7
    """
    stem = Path(str(source_cql_path or "").strip()).stem.strip().lower()
    family = str(measure_family_lower or "").strip().lower()
    if not stem:
        return ""

    if stem.endswith("adm"):
        stem = stem[:-3]

    if family and stem.startswith(family):
        return stem

    numeric_prefix_match = re.match(r"^(\d+)(.*)$", stem)
    if numeric_prefix_match:
        number = numeric_prefix_match.group(1).zfill(3)
        suffix = numeric_prefix_match.group(2)
        return f"{family}{number}{suffix}" if family else f"{number}{suffix}"

    return stem


def _resolve_source_cql_path(
    intake: dict[str, Any],
    *,
    measure_slug: str,
    measure_family_lower: str,
    preferred_source_cql_path: str = "",
) -> str:
    """Resolve the best matching source CQL path for a canonical measure slug."""
    cql_paths = [
        str(path).strip()
        for path in (intake.get("cqlPaths") or [])
        if str(path).strip()
    ]
    preferred = str(preferred_source_cql_path or "").strip()
    normalized_slug = str(measure_slug or "").strip().lower()

    if preferred:
        normalized_preferred = preferred.replace("\\", "/").lower()
        for candidate in cql_paths:
            if candidate.replace("\\", "/").lower() == normalized_preferred:
                return candidate
        if Path(preferred).name:
            preferred_name = Path(preferred).name.lower()
            for candidate in cql_paths:
                if Path(candidate).name.lower() == preferred_name:
                    return candidate

    if normalized_slug:
        for candidate in cql_paths:
            candidate_slug = _canonical_measure_slug_from_cql_path(
                candidate,
                measure_family_lower,
            )
            if candidate_slug == normalized_slug:
                return candidate

    if preferred:
        return preferred
    return cql_paths[0] if cql_paths else ""


def _detect_shared_cql_pattern(
    cql_paths: list[str],
    measure_family_lower: str,
) -> tuple[str, bool, str]:
    """Detect if there's a shared CQL file for a measure family.
    
    Args:
        cql_paths: List of CQL file paths from intake.cqlPaths
        measure_family_lower: Lowercase measure family (e.g., "hedis")
    
    Returns:
        Tuple of (family_prefix, has_shared, shared_cql_path)
        - family_prefix: Common prefix extracted from multiple measures (e.g., "TRC" from TRCA, TRCB)
        - has_shared: True if a shared CQL file exists in the paths
        - shared_cql_path: Full path to the shared CQL file if it exists, empty otherwise
    
    Examples:
        Input: ["/measures/2026/HEDIS/TRCA.cql", "/measures/2026/HEDIS/TRCB.cql", "/measures/2026/HEDIS/TRCshared.cql"]
        Output: ("TRC", True, "/measures/2026/HEDIS/TRCshared.cql")
        
        Input: ["/measures/2026/HEDIS/URI.cql"]
        Output: ("URI", False, "")
    """
    if not cql_paths:
        return "", False, ""
    
    # Extract filenames without extension
    filenames = [Path(p).stem for p in cql_paths]
    
    # Check for explicit *shared.cql or *Shared.cql files
    shared_candidates = [
        (p, Path(p).stem) 
        for p in cql_paths 
        if Path(p).stem.lower().endswith("shared")
    ]
    
    if shared_candidates:
        shared_path, shared_stem = shared_candidates[0]
        # Extract family prefix by removing "shared" suffix (case-insensitive)
        family_prefix = re.sub(r"shared$", "", shared_stem, flags=re.IGNORECASE)
        return family_prefix, True, shared_path
    
    # No shared file found - find the true common prefix across all filenames
    # e.g., TRCA, TRCB, TRCE, TRCE7, TRCM → common prefix "TRC"
    upper_filenames = [f.upper() for f in filenames]
    if len(upper_filenames) > 1:
        common = upper_filenames[0]
        for fname in upper_filenames[1:]:
            # Shorten common until it's a prefix of fname
            while not fname.startswith(common):
                common = common[:-1]
                if not common:
                    break
        # Trim trailing digits so we get "TRC" not "TRCE" from "TRCE"+"TRCA"
        family_prefix = re.sub(r"[0-9]+$", "", common).rstrip()
        if family_prefix:
            # has_shared=True when multiple CQL files share the same prefix:
            # the shared logic exists in the DRL layer (not as a shared CQL file)
            has_shared_drl_only = len(upper_filenames) > 1
            return family_prefix, has_shared_drl_only, ""

    # Single file or no clear pattern - strip trailing digits/letters to get base
    single = re.sub(r"[0-9]+$", "", upper_filenames[0]) if upper_filenames else ""
    return single if single else (filenames[0] if filenames else ""), False, ""


def _derive_measure_context(
    ticket_id: str,
    measure_slug: str = "",
    current_year: str = "",
    source_cql_path: str = "",
) -> dict[str, Any]:
    """Derive measure identifiers from config and intake artifacts."""
    config = _load_json_file(ORCHESTRATION_CONFIG_PATH)

    intake_path = ROOT_DIR / "artifacts" / ticket_id / "intake.json"
    if not intake_path.exists():
        raise RuntimeError(f"Missing intake artifact: {intake_path}")
    intake = _load_json_file(intake_path)

    cql_paths: list[str] = []
    for path in (intake.get("cqlPaths") or []):
        normalized_path = str(path).strip()
        if normalized_path:
            cql_paths.append(normalized_path)
    first_cql = cql_paths[0] if cql_paths else ""
    preferred_source_cql_path = str(source_cql_path).strip()
    source_cql_seed = preferred_source_cql_path or first_cql

    # Year fallback order: explicit argument -> cql path token -> intake.measureYear.
    year = str(current_year).strip()
    if not year and source_cql_seed:
        year_match = re.search(r"/(20\d{2})/", source_cql_seed)
        year = year_match.group(1) if year_match else ""
    if not year:
        year = str(intake.get("measureYear") or "").strip()
    if not year:
        raise RuntimeError(
            "Unable to derive current measurement year; provide current_year or intake.measureYear"
        )

    # Family fallback order: cql path token -> intake.measureFamily -> config default.
    family_match = re.search(r"/20\d{2}/([^/]+)/", source_cql_seed) if source_cql_seed else None
    intake_family = str(intake.get("measureFamily") or "").strip().lower()
    default_family = str((config.get("measureFamilies") or {}).get("default") or "mips").lower()
    if family_match:
        family = family_match.group(1).strip().lower()
    else:
        family = intake_family or default_family

    slug = str(measure_slug).strip().lower()
    if not slug and source_cql_seed:
        # Prioritize extracting from CQL path before falling back to intake.measureSlug
        slug = _canonical_measure_slug_from_cql_path(source_cql_seed, family)
    if not slug:
        # Only use intake.measureSlug as last resort (it's often a family/group slug like "trc")
        slug = str(intake.get("measureSlug") or "").strip().lower()

    if not slug:
        raise RuntimeError(
            "Unable to derive measure slug; provide measure_slug or intake.measureSlug"
        )
    if not _is_safe_slug(slug):
        raise RuntimeError(f"Invalid measure slug: {slug!r}")

    resolved_source_cql_path = _resolve_source_cql_path(
        intake,
        measure_slug=slug,
        measure_family_lower=family,
        preferred_source_cql_path=preferred_source_cql_path,
    )

    github_repo = config.get("githubRepo") or {}
    owner = str(github_repo.get("owner") or "").strip()
    repo = str(github_repo.get("repo") or "").strip()
    base_branch = str(github_repo.get("baseBranch") or "main").strip() or "main"
    drl_target_path = str(github_repo.get("drlTargetPath") or "").strip().strip("/")
    if not owner or not repo or not drl_target_path:
        raise RuntimeError("githubRepo owner/repo/drlTargetPath are required in config")

    return {
        "ticketId": ticket_id,
        "currentYear": year,
        "measureFamilyLower": family,
        "measureSlug": slug,
        "sourceCqlPath": resolved_source_cql_path,
        "owner": owner,
        "repo": repo,
        "baseBranch": base_branch,
        "drlTargetPath": drl_target_path,
        "intake": intake,
    }


def _replace_year_occurrences(content: str, old_year: str, new_year: str) -> tuple[str, int]:
    """Roll year tokens forward, including embedded ones (Year2025Trca, year2025, measure.Hedis.2025.X).

    Digit-dot neighbours are skipped so OID segments such as 113883.2025.1 are never rewritten.
    """
    if not old_year or old_year == new_year:
        return content, 0
    pattern = re.compile(rf"(?<!\d)(?<!\d\.){re.escape(old_year)}(?!\d)(?!\.\d)")
    return pattern.subn(new_year, content)


def _normalize_baseline_source_path(
    source_path: str,
    *,
    expected_paths: list[str],
    normalized_source_year: str,
    drl_target_path: str,
    measure_family_lower: str,
    measure_slug: str,
) -> str:
    """Normalize source path from either repo-relative path or tool cache/local file path."""
    candidate = str(source_path or "").strip().replace("\\", "/")
    if not candidate:
        return f"{drl_target_path}/{measure_family_lower}/year{normalized_source_year}/{measure_slug}.drl"

    if candidate in expected_paths:
        return candidate

    lowered = candidate.lower()
    suffix_matches = [path for path in expected_paths if lowered.endswith(path.lower())]
    if suffix_matches:
        return suffix_matches[0]

    return candidate


def _coerce_source_content(source_content: str, source_path: str) -> tuple[str, str]:
    """Resolve source content text, optionally loading from a provided local file path."""
    content = str(source_content or "")
    if content.strip():
        return content, "direct"

    path_hint = str(source_path or "").strip().strip('"')
    if not path_hint:
        return "", "missing"

    local_path = Path(path_hint)
    if not local_path.is_absolute():
        local_path = (ROOT_DIR / local_path).resolve()
    else:
        local_path = local_path.resolve()

    # Only DRL files inside this bundle or a real rules-engine checkout; anything else falls back to GitHub.
    allowed_roots = [ROOT_DIR.resolve()]
    rules_root = _resolve_rules_engine_root()
    if (rules_root / "src/main/resources/com/ablehealth/measure").is_dir():
        allowed_roots.append(rules_root.resolve())
    if local_path.suffix.lower() != ".drl" or not any(local_path.is_relative_to(root) for root in allowed_roots):
        return "", "missing"

    if local_path.exists() and local_path.is_file():
        return local_path.read_text(encoding="utf-8", errors="replace"), f"file:{local_path}"

    return "", "missing"


def _github_token() -> str:
    for name in (
        "GITHUB_TOKEN_HC",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_MCP_ACCESS_TOKEN",
    ):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _github_fetch_file_text(
    *,
    owner: str,
    repo: str,
    path: str,
    ref: str,
) -> tuple[bool, str, str]:
    """Fetch a text file from GitHub contents API using GITHUB_TOKEN."""
    token = _github_token()
    if not token:
        return False, "", "GITHUB_TOKEN_HC (or GITHUB_PERSONAL_ACCESS_TOKEN) environment variable is required"

    encoded_path = urllib.parse.quote(str(path).lstrip("/"), safe="/")
    encoded_ref = urllib.parse.quote(str(ref).strip() or "main")
    url = (
        f"https://api.github.com/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"
        f"/contents/{encoded_path}?ref={encoded_ref}"
    )

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            encoding = str(payload.get("encoding") or "").lower()
            content = payload.get("content")

            if isinstance(content, str) and content.strip():
                if encoding == "base64":
                    raw = base64.b64decode(content.encode("ascii"), validate=False)
                    return True, raw.decode("utf-8", errors="replace"), ""
                return True, content, ""

            download_url = str(payload.get("download_url") or "").strip()
            if download_url:
                download_req = urllib.request.Request(download_url)
                download_req.add_header("Authorization", f"Bearer {token}")
                with urllib.request.urlopen(download_req, timeout=120) as dl_resp:
                    body = dl_resp.read()
                    return True, body.decode("utf-8", errors="replace"), ""

            return False, "", "GitHub response did not include file content"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, "", "not-found"
        return False, "", f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, "", str(e)


def _github_get_default_branch(owner: str, repo: str) -> tuple[bool, str, str]:
    """Resolve repository default branch via GitHub API using GITHUB_TOKEN."""
    token = _github_token()
    if not token:
        return False, "", "GITHUB_TOKEN_HC (or GITHUB_PERSONAL_ACCESS_TOKEN) environment variable is required"

    url = (
        f"https://api.github.com/repos/{urllib.parse.quote(owner)}/"
        f"{urllib.parse.quote(repo)}"
    )

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            branch = str(payload.get("default_branch") or "").strip()
            if not branch:
                return False, "", "GitHub response did not include default_branch"
            return True, branch, ""
    except urllib.error.HTTPError as e:
        return False, "", f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, "", str(e)


def _github_get_file_metadata(
    *,
    owner: str,
    repo: str,
    path: str,
    ref: str,
) -> tuple[bool, dict[str, Any], str]:
    """Fetch GitHub contents metadata for one path on one ref."""
    token = _github_token()
    if not token:
        return False, {}, "GITHUB_TOKEN_HC (or GITHUB_PERSONAL_ACCESS_TOKEN) environment variable is required"

    encoded_path = urllib.parse.quote(str(path).lstrip("/"), safe="/")
    encoded_ref = urllib.parse.quote(str(ref).strip() or "main")
    url = (
        f"https://api.github.com/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"
        f"/contents/{encoded_path}?ref={encoded_ref}"
    )

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return True, payload, ""
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, {}, "not-found"
        return False, {}, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, {}, str(e)


def _github_git_json(
    method: str,
    owner: str,
    repo: str,
    endpoint: str,
    payload: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any], str]:
    """Make one authenticated Git Data API request and return (ok, json, error)."""
    token = _github_token()
    if not token:
        return False, {}, "GITHUB_TOKEN_HC (or GITHUB_PERSONAL_ACCESS_TOKEN) environment variable is required"
    url = (
        f"https://api.github.com/repos/{urllib.parse.quote(owner)}/"
        f"{urllib.parse.quote(repo)}/{endpoint}"
    )
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=str(method).upper())
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8")
            return True, (json.loads(body) if body.strip() else {}), ""
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        return False, {}, f"HTTP {e.code}: {e.reason} {err_body[:800]}".strip()
    except Exception as e:
        return False, {}, str(e)


def _github_commit_files_single(
    *,
    owner: str,
    repo: str,
    branch: str,
    files: list[tuple[str, str]],
    message: str,
) -> tuple[bool, dict[str, Any], str]:
    """Commit several text files to a branch in ONE commit via the Git Data API.

    `files` is a list of (repo_path, content) tuples. All content is sent as raw UTF-8 blobs
    (never base64-truncated, never pasted through a model). Returns (ok, info, error).
    """
    if not files:
        return False, {}, "no files to commit"
    branch_ref = urllib.parse.quote(branch, safe="/-_.")

    ok, ref, err = _github_git_json("GET", owner, repo, f"git/ref/heads/{branch_ref}")
    if not ok:
        return False, {}, f"resolve branch head failed: {err}"
    head_sha = str(((ref or {}).get("object") or {}).get("sha") or "").strip()
    if not head_sha:
        return False, {}, f"branch '{branch}' has no head sha (does it exist?)"

    ok, head_commit, err = _github_git_json("GET", owner, repo, f"git/commits/{head_sha}")
    if not ok:
        return False, {}, f"read head commit failed: {err}"
    base_tree_sha = str(((head_commit or {}).get("tree") or {}).get("sha") or "").strip()
    if not base_tree_sha:
        return False, {}, "head commit has no base tree sha"

    tree_items: list[dict[str, Any]] = []
    for repo_path, content in files:
        ok, blob, err = _github_git_json(
            "POST", owner, repo, "git/blobs", {"content": content, "encoding": "utf-8"}
        )
        if not ok:
            return False, {}, f"create blob for {repo_path} failed: {err}"
        blob_sha = str((blob or {}).get("sha") or "").strip()
        if not blob_sha:
            return False, {}, f"blob for {repo_path} returned no sha"
        tree_items.append(
            {"path": repo_path.lstrip("/"), "mode": "100644", "type": "blob", "sha": blob_sha}
        )

    ok, new_tree, err = _github_git_json(
        "POST", owner, repo, "git/trees", {"base_tree": base_tree_sha, "tree": tree_items}
    )
    if not ok:
        return False, {}, f"create tree failed: {err}"
    new_tree_sha = str((new_tree or {}).get("sha") or "").strip()
    if not new_tree_sha:
        return False, {}, "create tree returned no sha"

    # Empty-commit guard: if the freshly-built tree is identical to the branch's current tree, then
    # none of the supplied files actually differ from what is already on the branch. Skip creating the
    # commit so we never produce an empty "message-only" commit.
    if new_tree_sha == base_tree_sha:
        return (
            True,
            {
                "commitSha": head_sha,
                "treeSha": base_tree_sha,
                "parentSha": head_sha,
                "fileCount": 0,
                "noChanges": True,
            },
            "",
        )

    ok, new_commit, err = _github_git_json(
        "POST",
        owner,
        repo,
        "git/commits",
        {"message": message, "tree": new_tree_sha, "parents": [head_sha]},
    )
    if not ok:
        return False, {}, f"create commit failed: {err}"
    new_commit_sha = str((new_commit or {}).get("sha") or "").strip()
    if not new_commit_sha:
        return False, {}, "create commit returned no sha"

    ok, _, err = _github_git_json(
        "PATCH",
        owner,
        repo,
        f"git/refs/heads/{branch_ref}",
        {"sha": new_commit_sha, "force": False},
    )
    if not ok:
        return False, {}, f"update branch ref failed: {err}"

    return (
        True,
        {
            "commitSha": new_commit_sha,
            "treeSha": new_tree_sha,
            "parentSha": head_sha,
            "fileCount": len(files),
        },
        "",
    )


def _librarian_basic_token() -> str:
    """Resolve the Librarian Basic auth token from env (direct token or key/secret pair)."""
    token = os.environ.get("API_AUTH_BASIC_TOKEN", "").strip()
    if token:
        return token
    key = os.environ.get("LIBRARIAN_API_KEY", "").strip()
    secret = os.environ.get("LIBRARIAN_API_SECRET", "").strip()
    if key and secret:
        return base64.b64encode(f"{key}:{secret}".encode("utf-8")).decode("ascii")
    return ""


def _librarian_login(auth_cfg: dict[str, Any]) -> tuple[bool, str, str]:
    """Authenticate to the Librarian API and return a bearer token."""
    basic_token = _librarian_basic_token()
    if not basic_token:
        return False, "", (
            "Librarian auth token missing: set API_AUTH_BASIC_TOKEN, or LIBRARIAN_API_KEY + "
            "LIBRARIAN_API_SECRET."
        )
    url = str(auth_cfg.get("url") or "").strip()
    if not url:
        return False, "", "apiPayload.auth.url is required in config"
    headers = dict(auth_cfg.get("headers") or {})
    headers["Authorization"] = f"Basic {basic_token}"
    req = urllib.request.Request(url, data=b"", method=str(auth_cfg.get("method") or "POST"))
    for key, value in headers.items():
        req.add_header(key, str(value))
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return False, "", f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, "", str(e)
    token_path = str(auth_cfg.get("tokenJsonPath") or "access_token").strip()
    token = str(payload.get(token_path) or payload.get("access_token") or payload.get("token") or "").strip()
    if not token:
        return False, "", f"login response missing token (keys: {list(payload.keys())})"
    return True, token, ""


@mcp.tool()
def fetch_librarian_test_json(
    ticket_id: str = "",
    measure_slug: str = "",
    test_version: str = "published",
    source_cql_path: str = "",
    current_year: str = "",
) -> dict[str, Any]:
    """Download a measure's test JSON from the Librarian API and write it to pr-assets/<slug>.json.

    Replaces the old placeholder behavior. Uses orchestration config `apiPayload` for the auth and
    data endpoints, authenticating with API_AUTH_BASIC_TOKEN (or LIBRARIAN_API_KEY + LIBRARIAN_API_SECRET).
    Fails with a clear error if the Librarian returns no JSON — never writes a placeholder.

    Args:
        ticket_id: Ticket ID (resolves from run-input.json when omitted).
        measure_slug: Measure slug, e.g. trca. Prefer passing source_cql_path so it is derived.
        test_version: "published" (default) or "draft".
        source_cql_path: Full CQL path from intake.cqlPaths; the slug is derived from its filename.
        current_year: Override the measurement year (default: derived from context).
    """
    try:
        resolved_ticket = _resolve_ticket_id(ticket_id)
        context = _derive_measure_context(
            resolved_ticket,
            measure_slug,
            current_year,
            source_cql_path=source_cql_path,
        )
        slug = context["measureSlug"]
        family = context["measureFamilyLower"]
        year = str(context["currentYear"])
        year_short = year[-2:]

        config = _load_json_file(ORCHESTRATION_CONFIG_PATH)
        api_payload = config.get("apiPayload") or {}
        if not api_payload.get("enabled", True):
            return {"ok": False, "error": "apiPayload is disabled in config"}
        auth_cfg = api_payload.get("auth") or {}
        data_cfg = api_payload.get("data") or {}

        version = str(test_version or "published").strip().lower()
        if version == "draft":
            url_template = str(data_cfg.get("draftUrlTemplate") or "").strip()
        else:
            url_template = str(data_cfg.get("urlTemplate") or "").strip()
        if not url_template:
            return {"ok": False, "error": f"no Librarian URL template for test_version '{version}'"}

        spec_format = str(
            data_cfg.get("measureSpecificationFormat")
            or "{measureYearShort}-{measureFamilyLower}-{measureSlug}"
        )
        measure_specification = (
            spec_format
            .replace("{measureYearShort}", year_short)
            .replace("{measureFamilyLower}", family)
            .replace("{measureSlug}", slug)
        )
        url = url_template.replace("{measureSpecification}", measure_specification)

        login_ok, bearer, login_err = _librarian_login(auth_cfg)
        if not login_ok:
            return {"ok": False, "error": f"Librarian login failed: {login_err}"}

        headers = dict(data_cfg.get("headers") or {})
        auth_from = data_cfg.get("authorizationFromAuthResponse") or {}
        header_name = str(auth_from.get("headerName") or "Authorization")
        header_value_template = str(auth_from.get("headerValueTemplate") or "Bearer {token}")
        headers[header_name] = header_value_template.replace("{token}", bearer)

        req = urllib.request.Request(url, method=str(data_cfg.get("method") or "GET"))
        for key, value in headers.items():
            req.add_header(key, str(value))
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return {
                "ok": False,
                "measureSpecification": measure_specification,
                "url": url,
                "error": f"Librarian data request failed: HTTP {e.code}: {e.reason}",
            }
        except Exception as e:
            return {"ok": False, "url": url, "error": f"Librarian data request failed: {e}"}

        try:
            parsed = json.loads(raw)
        except Exception as e:
            return {"ok": False, "url": url, "error": f"Librarian response was not valid JSON: {e}"}
        if not isinstance(parsed, (dict, list)) or (isinstance(parsed, (dict, list)) and not parsed):
            return {
                "ok": False,
                "url": url,
                "error": "Librarian returned an empty JSON body — no placeholder written.",
            }

        output_dir = ROOT_DIR / "artifacts" / resolved_ticket / "pr-assets"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_name = str(data_cfg.get("outputFilePattern") or "{measureSlug}.json").replace(
            "{measureSlug}", slug
        )
        output_path = output_dir / output_name
        output_path.write_text(json.dumps(parsed, indent=2), encoding="utf-8")

        return {
            "ok": True,
            "ticketId": resolved_ticket,
            "measureSlug": slug,
            "measureSpecification": measure_specification,
            "testVersion": version,
            "url": url,
            "outputPath": str(output_path.relative_to(ROOT_DIR)).replace("\\", "/"),
            "source": f"librarian:{version}:{measure_specification}",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def read_rules_engine_file(
    path: str,
    owner: str = "",
    repo: str = "",
    ref: str = "main",
) -> dict[str, Any]:
    """Read a repo-relative file from the local rules-engine working copy, GitHub as fallback.

    The DRL pipeline bundle is a submodule of the rules-engine repo, so prior-year templates
    (e.g. Java tests under src/test/java/...) already exist on disk. This reads them locally
    first (no GITHUB_TOKEN, no network) and only falls back to the GitHub API when the local
    file is absent.

    Args:
        path: Repo-relative file path, e.g. src/test/java/com/ablehealth/measures/hedis/year2025/Year2025HedisTrcaTest.java
        owner: GitHub owner for fallback (defaults to orchestration config githubRepo.owner)
        repo: GitHub repo for fallback (defaults to orchestration config githubRepo.repo)
        ref: Branch/ref for the GitHub fallback (default: main)
    """
    normalized_path = str(path or "").strip().lstrip("/")
    if not normalized_path:
        return {"ok": False, "error": "path is required"}

    # Local-first read from the rules-engine working copy.
    rules_engine_root = _resolve_rules_engine_root()
    local_path = (rules_engine_root / normalized_path).resolve()
    try:
        # Containment: never read outside the rules-engine root.
        local_path.relative_to(rules_engine_root.resolve())
        if local_path.is_file():
            text = local_path.read_text(encoding="utf-8-sig")
            return {
                "ok": True,
                "path": normalized_path,
                "content": text,
                "contentLength": len(text),
                "source": f"local:{local_path}",
            }
    except Exception:
        pass

    # Fallback to GitHub when the file is not present locally.
    config = _load_json_file(ORCHESTRATION_CONFIG_PATH)
    github_repo = config.get("githubRepo") or {}
    fallback_owner = str(owner or github_repo.get("owner") or "").strip()
    fallback_repo = str(repo or github_repo.get("repo") or "").strip()
    normalized_ref = str(ref or github_repo.get("baseBranch") or "main").strip() or "main"
    if not fallback_owner or not fallback_repo:
        return {
            "ok": False,
            "path": normalized_path,
            "error": (
                "File not found in local rules-engine working copy and no GitHub owner/repo "
                "available for fallback."
            ),
            "localPathAttempted": str(local_path),
        }

    ok, text, err = _github_fetch_file_text(
        owner=fallback_owner,
        repo=fallback_repo,
        path=normalized_path,
        ref=normalized_ref,
    )
    if ok:
        return {
            "ok": True,
            "path": normalized_path,
            "content": text,
            "contentLength": len(text),
            "source": f"github-api:{normalized_ref}:{normalized_path}",
        }
    return {
        "ok": False,
        "path": normalized_path,
        "error": err,
        "localPathAttempted": str(local_path),
        "githubRef": normalized_ref,
    }


# Placeholder/truncation markers that must never reach a PR (assets and DRLs alike).
_PUBLISH_TRUNCATION_MARKERS = (
    "omitted for brevity",
    "content omitted",
    "truncated for brevity",
    "simplified placeholder",
    "rest of baseline",
    "unchanged baseline rules",
    "<copied from",
    "... (rest",
    "// ...",
)

# Non-DRL/JSON/test repo files the converter may edit in the local rules-engine working copy (e.g.
# registering a measure for the maxCoverageDateByPayer global). Each is published ONLY when its local
# content differs from the remote PR branch. Override via config githubRepo.extraConfigFiles.
_DEFAULT_EXTRA_CONFIG_FILES = (
    "src/main/resources/config_load_rules/MapperUseEnrollmentsRules.json",
    "src/main/resources/config_load_rules/MapperSharedRules.json",
    "src/main/resources/config_load_rules/MapperUseProviderRules.json",
)


def _mapper_key_slug(key: str) -> str:
    """`measure/hedis/year2026/trca.drl` -> `trca` (lowercased, no .drl)."""
    base = str(key).rsplit("/", 1)[-1].strip().lower()
    if base.endswith(".drl"):
        base = base[:-4]
    return base


def _collect_ticket_mapper_entries(
    node: Any, slugs: set[str], path: tuple = ()
) -> list[tuple[tuple, str, Any]]:
    """Find every dict key that registers one of this ticket's measure slugs.

    Returns (parent_path, key, value) tuples. Only the KEY is matched against ticket slugs, so a
    measure's value object (e.g. {"rulesPath": [...]}) is never mistaken for a registration.
    """
    out: list[tuple[tuple, str, Any]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and _mapper_key_slug(k) in slugs:
                out.append((path, k, v))
            else:
                out.extend(_collect_ticket_mapper_entries(v, slugs, path + (k,)))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            out.extend(_collect_ticket_mapper_entries(item, slugs, path + (i,)))
    return out


def _get_at_path(obj: Any, path: tuple) -> Any:
    cur = obj
    for p in path:
        if isinstance(cur, dict) and isinstance(p, str) and p in cur:
            cur = cur[p]
        elif isinstance(cur, list) and isinstance(p, int) and 0 <= p < len(cur):
            cur = cur[p]
        else:
            return None
    return cur


def _find_object_span(text: str, container_key: str) -> tuple[int, int] | None:
    """Return (open_brace_idx, close_brace_idx) of the object value for a top-level key."""
    m = re.search(r'"' + re.escape(container_key) + r'"\s*:\s*\{', text)
    if not m:
        return None
    open_idx = text.index("{", m.end() - 1)
    depth = 0
    in_str = False
    esc = False
    i = open_idx
    while i < len(text):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return open_idx, i
        i += 1
    return None


def _textual_mapper_insert(
    remote_text: str, missing: list[tuple[tuple, str, Any]]
) -> str | None:
    """Minimal-diff insert of new ticket entries into the remote mapper text (no reformat).

    Only supports entries sharing a single top-level parent key (e.g. MEASURES); returns None to
    signal the caller should fall back to a full re-serialize.
    """
    parents = {path for (path, _, _) in missing}
    if len(parents) != 1:
        return None
    parent = next(iter(parents))
    if len(parent) != 1 or not isinstance(parent[0], str):
        return None
    span = _find_object_span(remote_text, parent[0])
    if not span:
        return None
    open_idx, close_idx = span
    has_content = bool(remote_text[open_idx + 1 : close_idx].strip())
    j = close_idx - 1
    while j > open_idx and remote_text[j] in " \t\r\n":
        j -= 1
    insert_at = j + 1
    parts: list[str] = []
    for idx, (_, key, value) in enumerate(missing):
        compact = json.dumps(value, separators=(", ", ": "))
        is_last = (idx == len(missing) - 1)
        trailing = "," if not is_last else ""
        parts.append(f"\n    {json.dumps(key)}: {compact}{trailing}")
    # Separator: add a trailing comma after the last existing entry rather than a
    # leading comma before each new entry, so the result matches the file's style.
    separator = "," if has_content else ""
    return remote_text[:insert_at] + separator + "".join(parts) + remote_text[insert_at:]


def _build_mapper_push_content(
    *,
    local_text: str,
    remote_ok: bool,
    remote_text: str,
    remote_not_found: bool,
    ticket_slugs: set[str],
) -> tuple[str, str | None, str]:
    """Decide whether/what to push for one mapper file, scoped to THIS ticket's registrations.

    Returns (action, content, reason) where action is 'push', 'skip', or 'error'. The pushed content
    is always the REMOTE branch file with only this ticket's missing registration entries injected —
    never the whole local file — so unrelated branch/main drift is never carried and entries are never
    dropped.
    """
    try:
        local_obj = json.loads(local_text)
    except Exception as e:
        return ("skip", None, f"local mapper is not valid JSON ({e}); not pushed")

    ticket_entries = _collect_ticket_mapper_entries(local_obj, ticket_slugs)
    if not ticket_entries:
        return ("skip", None, "no ticket-related registration entries in local mapper")

    if remote_not_found:
        return ("skip", None, "remote mapper not found on branch; refusing to push a partial mapper")
    if not remote_ok:
        return ("error", None, "could not read remote mapper to merge ticket entries")
    try:
        remote_obj = json.loads(remote_text)
    except Exception as e:
        return ("error", None, f"remote mapper is not valid JSON ({e})")

    missing: list[tuple[tuple, str, Any]] = []
    for path, key, value in ticket_entries:
        container = _get_at_path(remote_obj, path)
        if not isinstance(container, dict) or key not in container or container.get(key) != value:
            missing.append((path, key, value))
    if not missing:
        return ("skip", None, "ticket registration already present on remote branch")

    merged = copy.deepcopy(remote_obj)
    for path, key, value in missing:
        cur = merged
        ok = True
        for p in path:
            nxt = cur.get(p) if isinstance(cur, dict) else None
            if not isinstance(nxt, dict):
                if isinstance(cur, dict):
                    cur[p] = {}
                    nxt = cur[p]
                else:
                    ok = False
                    break
            cur = nxt
        if ok and isinstance(cur, dict):
            cur[key] = value

    text_content = _textual_mapper_insert(remote_text, missing)
    if text_content is not None:
        try:
            if json.loads(text_content) == merged:
                return ("push", text_content, f"added {len(missing)} ticket registration(s)")
        except Exception:
            pass
    return (
        "push",
        json.dumps(merged, indent=2) + "\n",
        f"added {len(missing)} ticket registration(s) (reformatted)",
    )


_ENROLLMENT_MAPPER_PATH = "src/main/resources/config_load_rules/MapperUseEnrollmentsRules.json"


def _enrollment_mapper_registration(remote_text: str, keys: list[str]) -> tuple[str, str | None, str]:
    """Add `"<key>": {"rulesPath": ["<key>"]}` under MEASURES for each missing key. Returns (action, content, reason)."""
    try:
        remote_obj = json.loads(remote_text)
    except Exception as e:
        return ("error", None, f"remote mapper is not valid JSON ({e})")
    measures = remote_obj.get("MEASURES")
    if not isinstance(measures, dict):
        return ("error", None, "remote mapper has no MEASURES object")
    missing = [(("MEASURES",), key, {"rulesPath": [key]}) for key in dict.fromkeys(keys) if key not in measures]
    if not missing:
        return ("skip", None, "max-coverage DRLs already registered on the branch")
    merged = copy.deepcopy(remote_obj)
    for _, key, value in missing:
        merged["MEASURES"][key] = value
    text_content = _textual_mapper_insert(remote_text, missing)
    if text_content is not None:
        try:
            if json.loads(text_content) == merged:
                return ("push", text_content, f"registered {len(missing)} max-coverage DRL(s)")
        except Exception:
            pass
    return ("push", json.dumps(merged, indent=2) + "\n", f"registered {len(missing)} max-coverage DRL(s) (reformatted)")


def _format_commit_message(ticket: str, message: str) -> str:
    """Enforce the rules-engine commit-message format: ``[AB#<ticket_number>] <message>``.

    Extracts the numeric ticket number from the ticket id (``AB#12345`` or ``12345`` -> ``12345``),
    strips any pre-existing ``[AB#...]`` prefix from the supplied message so the prefix is never
    duplicated, and falls back to a default subject when no message is supplied.
    """
    raw_ticket = str(ticket or "").strip()
    digits = "".join(re.findall(r"\d+", raw_ticket))
    ticket_number = digits or raw_ticket
    subject = re.sub(r"^\s*\[\s*AB#\s*[^\]]*\]\s*", "", str(message or ""), flags=re.IGNORECASE).strip()
    if not subject:
        subject = f"DRL update for ticket {raw_ticket}" if raw_ticket else "DRL update"
    return f"[AB#{ticket_number}] {subject}"


def _resolve_pr_branch_name(ticket: str, github_repo: dict[str, Any]) -> str:
    """Deterministically resolve the remote PR branch from config.branchPattern + ticket metadata.

    Reads the ticket's intake.json so the branch is identical no matter which agent resolves it.
    Family-level tickets have no single measure number, so ``{measureNumber}`` collapses to empty.
    """
    pattern = str(github_repo.get("branchPattern") or "").strip()
    if not pattern:
        return ""
    try:
        intake = _load_json_file(ROOT_DIR / "artifacts" / ticket / "intake.json")
    except Exception:
        intake = {}
    family_lower = str(intake.get("measureSlug") or intake.get("measureFamily") or "").strip().lower()
    year = str(intake.get("measureYear") or "").strip()
    values = {
        "measureFamilyLower": family_lower,
        "measureNumber": "",
        "measureYear": year,
        "ticketId": str(ticket),
    }
    branch = pattern
    for key, val in values.items():
        branch = branch.replace("{" + key + "}", val)
    # Collapse separators left behind by empty tokens (e.g. an empty measureNumber).
    branch = re.sub(r"-{2,}", "-", branch).strip().strip("/")
    return branch


def _github_ensure_branch(
    owner: str, repo: str, branch: str, base_branch: str
) -> tuple[bool, bool, str]:
    """Ensure ``branch`` exists remotely; create it from ``base_branch`` head when missing.

    Returns ``(ok, created, error)``. ``created`` is True only when a new branch was made. This lets
    the PR Submitter resume from any stage even if the Test Builder never created the branch.
    """
    branch_ref = urllib.parse.quote(branch, safe="/-_.")
    ok, ref, _ = _github_git_json("GET", owner, repo, f"git/ref/heads/{branch_ref}")
    if ok and str(((ref or {}).get("object") or {}).get("sha") or "").strip():
        return True, False, ""
    base_ref = urllib.parse.quote(base_branch, safe="/-_.")
    ok, bref, berr = _github_git_json("GET", owner, repo, f"git/ref/heads/{base_ref}")
    if not ok:
        return False, False, f"base branch '{base_branch}' not found: {berr}"
    base_sha = str(((bref or {}).get("object") or {}).get("sha") or "").strip()
    if not base_sha:
        return False, False, f"base branch '{base_branch}' has no head sha"
    ok, _, cerr = _github_git_json(
        "POST", owner, repo, "git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha}
    )
    if not ok:
        # Possible race — re-check whether the branch now exists before failing.
        ok2, ref2, _ = _github_git_json("GET", owner, repo, f"git/ref/heads/{branch_ref}")
        if ok2 and str(((ref2 or {}).get("object") or {}).get("sha") or "").strip():
            return True, False, ""
        return False, False, f"create branch '{branch}' failed: {cerr}"
    return True, True, ""


_NEW_YEAR_FILE_NOTE = "new (year file missing; unchanged vs baseline)"


def _current_year_drl_exists(
    *, owner: str, repo: str, repo_path: str, base_branch: str, meta: dict[str, Any]
) -> tuple[bool, str]:
    """Whether the current-year DRL already exists in rules-engine. Returns (exists, evidenceSource).

    Order: GitHub base branch (authoritative, case-sensitive) > local rules-engine checkout
    (exact-case name match) > baseline metadata currentYearDrlExisted. Unknown -> treated as existing,
    so an unchanged DRL is only published when its absence is positively established.
    """
    ok, _, err = _github_fetch_file_text(owner=owner, repo=repo, path=repo_path, ref=base_branch)
    if ok:
        return True, f"github:{base_branch}"
    if err == "not-found":
        return False, f"github:{base_branch}"

    rules_root = _resolve_rules_engine_root()
    drl_root = rules_root / "src/main/resources/com/ablehealth/measure"
    if drl_root.is_dir():
        local_path = rules_root / repo_path
        parent = local_path.parent
        try:
            names = {p.name for p in parent.iterdir()} if parent.is_dir() else set()
        except OSError:
            names = None
        if names is not None:
            return local_path.name in names, "local-rules-engine"

    if isinstance(meta, dict) and meta.get("currentYearDrlExisted") is False:
        return False, "baseline-metadata"
    return True, "unknown (assumed existing)"


@mcp.tool()
def publish_approved_pr_files(
    ticket_id: str = "",
    branch: str = "",
    owner: str = "",
    repo: str = "",
    message: str = "",
) -> dict[str, Any]:
    """Deterministically push ONLY the approved DRL changes + PR assets to the branch in ONE commit.

    This is the ONLY sanctioned way the PR Submitter writes repo files. It removes the failure mode
    that produced truncated code and multiple drastic commits, by enforcing:

    1. Approval gate — refuses unless approval.approved is true.
    2. Approved-changes-only — for every changed generated DRL it (a) structurally validates it
       against the baseline and (b) recomputes the baseline->generated diff and requires it to match
       the human-approved patches/<ticket>-<slug>-drl.patch. If the on-disk DRL drifted from what was
       approved, the push is refused. Unchanged DRLs (generated == baseline) are skipped when the
       current-year target file already exists; when it does NOT exist (checked on the GitHub base
       branch, then the local rules-engine checkout, then baseline currentYearDrlExisted=false) the
       DRL is still published as a new year file, labelled "new (year file missing; unchanged vs baseline)".
    3. Content-safe — every file is read off disk and sent as a raw UTF-8 blob; nothing is pasted
       through the model and nothing is truncated.
    4. Single commit — all files land in ONE commit via the Git Data API (no per-file commit churn).
    5. Ticket-scoped config mappers — for config_load_rules/Mapper*.json it pushes ONLY the registration
       entries whose key references one of THIS ticket's measure slugs and that are missing on the remote
       branch. The pushed file is the remote branch file + those ticket entries (never the whole local
       file), so a branch that lags main is never regressed and unrelated local drift is never carried.
       Any path listed in the ticket's pr-extra-files.json manifest is treated as an explicit ticket file
       and pushed whole only when it differs from the remote branch.
    6. No empty commits — if every gathered file already matches the branch, the resulting tree equals
       the branch tree and NO commit is created (returns noChanges=true instead of an empty commit).

    Args:
        ticket_id: Ticket id (resolves from run-input.json when omitted).
        branch: Remote PR branch to push to. OPTIONAL — when omitted it is resolved deterministically
            from config githubRepo.branchPattern + the ticket's intake metadata, and created from the
            base branch if it does not exist yet. Pass a value only to override the resolved name.
        owner: Repo owner (defaults to config githubRepo.owner).
        repo: Repo name (defaults to config githubRepo.repo).
        message: Commit subject (auto-generated when omitted). The final commit message is always
            normalized to ``[AB#<ticket_number>] <subject>``; an existing ``[AB#...]`` prefix is not
            duplicated.
    """
    try:
        ticket = _resolve_ticket_id(ticket_id)
        if not ticket:
            return {"ok": False, "error": "ticket_id is required"}

        approval = TicketStateManager.read_approval_status(ticket)
        if not bool((approval or {}).get("approved")):
            return {
                "ok": False,
                "error": "Approval gate failed: approval.approved is not true. Record approval first.",
                "approval": approval,
            }

        config = _load_json_file(ORCHESTRATION_CONFIG_PATH)
        gh = config.get("githubRepo") or {}
        owner = str(owner or gh.get("owner") or "").strip()
        repo = str(repo or gh.get("repo") or "").strip()
        if not owner or not repo:
            return {"ok": False, "error": "owner/repo not provided and not found in config.githubRepo"}

        # Resolve the remote PR branch deterministically (caller override wins), then make sure it
        # exists on the remote — creating it from the base branch when missing. This keeps the PR
        # Submitter self-contained so it never has to re-run upstream stages to "find" a branch.
        base_branch = str(gh.get("baseBranch") or "main").strip() or "main"
        target_branch = str(branch or "").strip() or _resolve_pr_branch_name(ticket, gh)
        if not target_branch:
            return {
                "ok": False,
                "error": "Could not resolve a PR branch name (pass branch=... or set githubRepo.branchPattern).",
            }
        protected_branches = {"main", "master", base_branch.lower()}
        if target_branch.removeprefix("refs/heads/").lower() in protected_branches:
            return {
                "ok": False,
                "error": f"Refusing to publish to protected branch '{target_branch}'. Use a feature branch.",
            }
        ensured, branch_created, branch_err = _github_ensure_branch(
            owner, repo, target_branch, base_branch
        )
        if not ensured:
            return {"ok": False, "error": f"Could not ensure branch '{target_branch}': {branch_err}"}

        drl_target = str(gh.get("drlTargetPath") or "src/main/resources/com/ablehealth/measure")
        test_target = str(gh.get("testTargetPath") or "")
        json_target = str(gh.get("jsonTargetPath") or "")

        ticket_root = ROOT_DIR / "artifacts" / ticket
        baseline_dir = ticket_root / "drls" / "baseline"
        generated_dir = ticket_root / "drls" / "generated"
        patches_dir = ticket_root / "patches"
        baseline_meta_dir = ticket_root / "baseline"
        pr_assets_dir = ticket_root / "pr-assets"

        if not generated_dir.exists():
            return {"ok": False, "error": f"No generated DRLs found at {generated_dir}"}

        def _meta(slug: str) -> dict[str, Any]:
            meta_path = baseline_meta_dir / f"{slug}-baseline.json"
            if meta_path.exists():
                try:
                    return json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    return {}
            return {}

        files: list[tuple[str, str]] = []
        published: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        measure_slugs: list[str] = []
        max_coverage_keys: list[str] = []

        # 1) Changed DRLs — validated against baseline + approved patch. Unchanged DRLs whose
        #    current-year file is missing in rules-engine are published as new year files.
        generated_by_slug: dict[str, list[str]] = {}
        for generated_path in generated_dir.glob("*.drl"):
            generated_by_slug.setdefault(_generated_drl_slug(generated_path), []).append(generated_path.name)
        duplicate_slugs = {s: sorted(n) for s, n in generated_by_slug.items() if len(n) > 1}
        if duplicate_slugs:
            return {
                "ok": False,
                "error": "Generated DRLs differ only by case; keep exactly one file per slug: "
                + "; ".join(f"{s}: {', '.join(n)}" for s, n in sorted(duplicate_slugs.items())),
            }

        for generated_path in sorted(generated_dir.glob("*.drl")):
            slug = _generated_drl_slug(generated_path)
            measure_slugs.append(slug)
            baseline_path = baseline_dir / f"{slug}.drl"
            if not baseline_path.exists():
                return {"ok": False, "error": f"Missing baseline DRL for '{slug}' — cannot verify changes."}

            baseline_text = baseline_path.read_text(encoding="utf-8", errors="replace")
            generated_text = generated_path.read_text(encoding="utf-8", errors="replace")

            unchanged = baseline_text == generated_text
            new_year_file = False
            if unchanged:
                meta = _meta(slug)
                family = str(meta.get("measureFamilyLower") or "").strip()
                year = str(meta.get("currentYear") or "").strip()
                if not family or not year:
                    skipped.append({"slug": slug, "reason": "unchanged (generated == baseline)"})
                    continue
                repo_path = f"{drl_target}/{family}/year{year}/{_baseline_target_file_name(meta, slug)}"
                exists, evidence = _current_year_drl_exists(
                    owner=owner, repo=repo, repo_path=repo_path, base_branch=base_branch, meta=meta
                )
                if exists:
                    skipped.append({
                        "slug": slug,
                        "reason": "unchanged (generated == baseline)",
                        "repoPath": repo_path,
                        "existenceCheck": evidence,
                    })
                    continue
                # The rolled-forward year file does not exist yet: it must still be created even
                # though no content edit was needed. There is no patch to verify (diff is empty).
                new_year_file = True

            integrity_ok, integrity_err = _validate_generated_drl_integrity(baseline_text, generated_text)
            if not integrity_ok:
                return {"ok": False, "error": f"DRL '{slug}' failed integrity check: {integrity_err}"}

            lowered = generated_text.lower()
            marker = next((m for m in _PUBLISH_TRUNCATION_MARKERS if m in lowered), "")
            if marker:
                return {"ok": False, "error": f"DRL '{slug}' contains placeholder/truncation text: '{marker}'"}

            if new_year_file:
                files.append((repo_path, generated_text))
                published.append({
                    "slug": slug,
                    "type": "drl",
                    "repoPath": repo_path,
                    "note": _NEW_YEAR_FILE_NOTE,
                    "existenceCheck": evidence,
                })
                if re.search(r"\bglobal\s+[^;]*\bmaxCoverageDateByPayer\b", generated_text):
                    max_coverage_keys.append(repo_path.split("/com/ablehealth/", 1)[-1])
                continue

            patch_path = patches_dir / f"{ticket}-{slug}-drl.patch"
            if not patch_path.exists():
                return {
                    "ok": False,
                    "error": f"DRL '{slug}' changed but has no approved patch ({patch_path.name}). "
                    "Only approved patch changes may be published.",
                }
            approved_patch = patch_path.read_text(encoding="utf-8", errors="replace")
            recomputed = _canonical_unified_diff_text(
                root=ROOT_DIR, baseline_path=baseline_path, generated_path=generated_path
            )
            if not _diff_matches_approved(recomputed, approved_patch):
                return {
                    "ok": False,
                    "error": f"DRL '{slug}' on disk has drifted from the approved patch "
                    f"({patch_path.name}). Refusing to publish unapproved changes. "
                    "Re-run conversion/approval or restore the approved generated DRL.",
                }

            meta = _meta(slug)
            family = str(meta.get("measureFamilyLower") or "").strip()
            year = str(meta.get("currentYear") or "").strip()
            if not family or not year:
                return {"ok": False, "error": f"Baseline metadata for '{slug}' lacks measureFamilyLower/currentYear."}
            # Current-year file name: targetFileName (original case) > sourcePath name > <slug>.drl.
            # sourcePath points at the prior-year file for rolled baselines; always publish to the current year.
            file_name = _baseline_target_file_name(meta, slug)
            repo_path = f"{drl_target}/{family}/year{year}/{file_name}"
            files.append((repo_path, generated_text))
            published.append({"slug": slug, "type": "drl", "repoPath": repo_path})
            if re.search(r"\bglobal\s+[^;]*\bmaxCoverageDateByPayer\b", generated_text):
                max_coverage_keys.append(repo_path.split("/com/ablehealth/", 1)[-1])

        # 1b) Enrollment mapper registration — every changed DRL that declares the maxCoverageDateByPayer
        #     global must be registered, otherwise the framework never populates it.
        if max_coverage_keys:
            mapper_path = _ENROLLMENT_MAPPER_PATH
            r_ok, remote_text, r_err = _github_fetch_file_text(
                owner=owner, repo=repo, path=mapper_path, ref=target_branch
            )
            if not r_ok:
                return {"ok": False, "error": f"Could not read {mapper_path} on '{target_branch}' to register max-coverage DRLs: {r_err}"}
            action, content, reason = _enrollment_mapper_registration(remote_text, max_coverage_keys)
            if action == "error":
                return {"ok": False, "error": f"{mapper_path}: {reason}"}
            if action == "push" and content is not None:
                files.append((mapper_path, content))
                published.append({"slug": Path(mapper_path).name, "type": "config", "repoPath": mapper_path, "note": reason})
            else:
                skipped.append({"repoPath": mapper_path, "reason": reason})

        # 2) PR assets — test JSON (per measure) and Java test files, read off disk.
        for slug in measure_slugs:
            json_path = pr_assets_dir / f"{slug}.json"
            if not json_path.exists():
                continue
            json_text = json_path.read_text(encoding="utf-8", errors="replace")
            marker = next((m for m in _PUBLISH_TRUNCATION_MARKERS if m in json_text.lower()), "")
            if marker:
                return {"ok": False, "error": f"Test JSON '{slug}.json' contains placeholder text: '{marker}'"}
            meta = _meta(slug)
            family = str(meta.get("measureFamilyLower") or "").strip()
            year = str(meta.get("currentYear") or "").strip()
            if not json_target:
                continue
            json_repo_dir = json_target.replace("{measureFamilyLower}", family).replace("{measureYear}", year)
            repo_path = f"{json_repo_dir}/{slug}.json"
            files.append((repo_path, json_text))
            published.append({"slug": slug, "type": "json", "repoPath": repo_path})

        for java_path in sorted(pr_assets_dir.glob("Year*Test.java")):
            java_text = java_path.read_text(encoding="utf-8", errors="replace")
            marker = next((m for m in _PUBLISH_TRUNCATION_MARKERS if m in java_text.lower()), "")
            if marker:
                return {"ok": False, "error": f"Test file '{java_path.name}' contains placeholder text: '{marker}'"}
            # Family/year derived from the first measure's metadata (same family/year for the bundle).
            meta = _meta(measure_slugs[0]) if measure_slugs else {}
            family = str(meta.get("measureFamilyLower") or "").strip()
            year = str(meta.get("currentYear") or "").strip()
            if not test_target:
                continue
            test_repo_dir = test_target.replace("{measureFamilyLower}", family).replace("{measureYear}", year)
            repo_path = f"{test_repo_dir}/{java_path.name}"
            files.append((repo_path, java_text))
            published.append({"slug": java_path.stem, "type": "test-java", "repoPath": repo_path})

        # 3) Extra modified repo files the converter edited in the local rules-engine working copy.
        #    Two kinds, handled differently so unrelated branch/main drift is never carried:
        #    (a) Mapper config files (config_load_rules/Mapper*.json) — TICKET-SCOPED. We only push when
        #        the converter added a registration entry whose key references one of THIS ticket's
        #        measure slugs AND that entry is missing on the remote branch. The pushed content is the
        #        remote file + only those ticket entries (never the whole local file), so a branch that
        #        is behind main is never regressed and unrelated local edits are never pushed.
        #    (b) Per-ticket manifest files (pr-extra-files.json) — explicit converter-declared ticket
        #        files; pushed whole when local differs from the remote branch.
        rules_root = _resolve_rules_engine_root()
        already_paths = {f[0] for f in files}
        ticket_slugs = {s.lower() for s in measure_slugs}

        mapper_candidates: list[str] = []
        config_extras = gh.get("extraConfigFiles")
        if isinstance(config_extras, list) and config_extras:
            mapper_candidates.extend(str(p).strip() for p in config_extras if str(p).strip())
        else:
            mapper_candidates.extend(_DEFAULT_EXTRA_CONFIG_FILES)

        seen_extra: set[str] = set()
        for raw_repo_path in mapper_candidates:
            repo_path = str(raw_repo_path).lstrip("/").strip()
            if not repo_path or repo_path in already_paths or repo_path in seen_extra:
                continue
            seen_extra.add(repo_path)
            local_path = rules_root / repo_path
            if not local_path.exists():
                skipped.append({"repoPath": repo_path, "reason": "not present in local rules-engine"})
                continue
            local_text = local_path.read_text(encoding="utf-8", errors="replace")
            r_ok, remote_text, r_err = _github_fetch_file_text(
                owner=owner, repo=repo, path=repo_path, ref=target_branch
            )
            action, content, reason = _build_mapper_push_content(
                local_text=local_text,
                remote_ok=r_ok,
                remote_text=remote_text,
                remote_not_found=(not r_ok and r_err == "not-found"),
                ticket_slugs=ticket_slugs,
            )
            if action == "error":
                return {"ok": False, "error": f"Mapper '{repo_path}': {reason} (git error details: {r_err})"}
            if action == "skip" or content is None:
                skipped.append({"repoPath": repo_path, "reason": reason})
                continue
            marker = next((m for m in _PUBLISH_TRUNCATION_MARKERS if m in content.lower()), "")
            if marker:
                return {
                    "ok": False,
                    "error": f"Mapper '{repo_path}' merge produced placeholder/truncation text: '{marker}'",
                }
            files.append((repo_path, content))
            published.append({"slug": Path(repo_path).name, "type": "config", "repoPath": repo_path, "note": reason})

        # (b) Manifest files — explicit, ticket-specific; whole-file compare against the remote branch.
        manifest_candidates: list[str] = []
        manifest_path = ticket_root / "pr-extra-files.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                entries = manifest.get("files") if isinstance(manifest, dict) else manifest
                if isinstance(entries, list):
                    for entry in entries:
                        if isinstance(entry, str):
                            p = entry.strip()
                        elif isinstance(entry, dict):
                            p = str(entry.get("repoPath") or entry.get("path") or "").strip()
                        else:
                            p = ""
                        if p:
                            manifest_candidates.append(p)
            except Exception:
                pass

        for raw_repo_path in manifest_candidates:
            repo_path = str(raw_repo_path).lstrip("/").strip()
            if not repo_path or repo_path in already_paths or repo_path in seen_extra:
                continue
            seen_extra.add(repo_path)
            local_path = rules_root / repo_path
            if not local_path.exists():
                skipped.append({"repoPath": repo_path, "reason": "not present in local rules-engine"})
                continue
            local_text = local_path.read_text(encoding="utf-8", errors="replace")
            marker = next((m for m in _PUBLISH_TRUNCATION_MARKERS if m in local_text.lower()), "")
            if marker:
                return {
                    "ok": False,
                    "error": f"Extra file '{repo_path}' contains placeholder/truncation text: '{marker}'",
                }
            r_ok, remote_text, r_err = _github_fetch_file_text(
                owner=owner, repo=repo, path=repo_path, ref=target_branch
            )
            if r_ok:
                if remote_text == local_text:
                    skipped.append({"repoPath": repo_path, "reason": "unchanged (matches remote branch)"})
                    continue
            elif r_err != "not-found":
                return {
                    "ok": False,
                    "error": f"Could not compare extra file '{repo_path}' against remote branch "
                    f"'{target_branch}': {r_err}",
                }
            files.append((repo_path, local_text))
            published.append({"slug": Path(repo_path).name, "type": "extra", "repoPath": repo_path})

        if not files:
            return {
                "ok": False,
                "error": "Nothing approved to publish (no changed DRLs and no assets found).",
                "skipped": skipped,
            }

        commit_message = _format_commit_message(ticket, message)
        ok, info, err = _github_commit_files_single(
            owner=owner, repo=repo, branch=target_branch, files=files, message=commit_message
        )
        if not ok:
            return {"ok": False, "error": f"Single-commit push failed: {err}", "intendedFiles": [f[0] for f in files]}

        no_changes = bool(info.get("noChanges"))
        return {
            "ok": True,
            "ticketId": ticket,
            "owner": owner,
            "repo": repo,
            "branch": target_branch,
            "branchCreated": branch_created,
            "noChanges": no_changes,
            "commitSha": info.get("commitSha"),
            "fileCount": info.get("fileCount"),
            "published": [] if no_changes else published,
            "skipped": skipped,
            "commitMessage": commit_message,
            "message": (
                "No commit created — remote branch already matches all approved artifacts."
                if no_changes
                else "Approved artifacts pushed in a single commit."
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def download_and_extract_cql(ticket_id: str = "") -> dict[str, Any]:
    """Download every intake CQL fully and build extraction.json in one deterministic step.

    This is the only authoritative producer of the raw CQL files and extraction.json.
    Agents must call this once instead of hand-writing or summarizing CQL content. It
    fails closed (non-zero) if any intake cqlPath is unresolved, empty, undecodable, or
    contains a truncation marker, so a success result guarantees complete CQL on disk.

    Step 1 (CQL diff): it also writes the PR's own change as a unified diff patch per
    changed CQL file (base commit -> head) to artifacts/<ticket>/patches/<slug>-cql-diff.patch.
    These patches reflect the actual merged change and are never empty; they do not depend
    on whether the PR is completed/merged. The next stage (check_drl_already_updated) uses
    them to decide whether the rules-engine DRL already contains the change.

    Args:
        ticket_id: Optional ticket id. If omitted, helper infers from run-input.json.
    """
    ticket = _resolve_ticket_id(ticket_id)
    ticket_root = ROOT_DIR / "artifacts" / ticket
    if ticket and not (ticket_root / "intake.json").exists():
        return {
            "ok": False,
            "returnCode": 1,
            "error": f"Missing artifacts/{ticket}/intake.json. Call write_intake_json first; do not delegate the write.",
        }
    args = ["scripts/ado_extraction_fallback.py", "download-and-extract"]
    if ticket:
        args.extend([
            "--ticket-id", ticket,
            "--intake", str(ticket_root / "intake.json"),
            "--raw-dir", str(ticket_root / "cqls" / "extraction"),
            "--out", str(ticket_root / "extraction-fetched.json"),
            "--extraction-out", str(ticket_root / "extraction.json"),
        ])
    return _run_python_script(args)


def _resolve_rules_engine_root() -> Path:
    """Resolve the local rules-engine working copy (the parent repo this bundle is a submodule of)."""
    drl_rel = "src/main/resources/com/ablehealth/measure"
    candidates: list[Path] = []
    env_root = os.environ.get("RULES_ENGINE_REPO_PATH", "").strip()
    if env_root:
        candidates.append(Path(env_root))
    # ROOT_DIR = <rules-engine>/measureable_content_agent/.github/agents/Drl Pipeline
    if len(ROOT_DIR.parents) > 3:
        candidates.append(ROOT_DIR.parents[3])
        # Standalone layout without Docker: rules-engine checked out next to this repo.
        candidates.append(ROOT_DIR.parents[3] / "rules-engine")
    for cand in candidates:
        try:
            if (cand / drl_rel).is_dir():
                return cand
        except Exception:
            continue
    return candidates[0] if candidates else ROOT_DIR.parent


def _distinctive_green_lines(patch_text: str) -> list[str]:
    """Extract whitespace-normalized added (green) CQL lines that are specific enough to match in a DRL."""
    seen: set[str] = set()
    lines: list[str] = []
    for raw in patch_text.splitlines():
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        norm = re.sub(r"\s+", " ", raw[1:]).strip()
        if len(norm) < 12 or not re.search(r"[A-Za-z]", norm):
            continue
        if norm in seen:
            continue
        seen.add(norm)
        lines.append(norm)
    return lines


def _distinctive_diff_lines(patch_text: str, sign: str) -> list[str]:
    """Whitespace-normalized changed CQL lines for a diff sign ('+' added, '-' removed).

    Used to detect a stale embedded CQL comment: a line the PR REMOVED from the CQL that
    still appears inside a DRL /* */ block means the commented CQL was not updated to match
    the converted rule logic.
    """
    prefix_skip = "+++" if sign == "+" else "---"
    seen: set[str] = set()
    out: list[str] = []
    for raw in patch_text.splitlines():
        if not raw.startswith(sign) or raw.startswith(prefix_skip):
            continue
        norm = re.sub(r"\s+", " ", raw[1:]).strip()
        if len(norm) < 12 or not re.search(r"[A-Za-z]", norm):
            continue
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


# Mirrors the rules-engine copilot-instructions.md PR-review rules; tags listed in .github/skills/drl-review-rules/SKILL.md.
_REVIEW_SUPP_KEYS = {
    "calculatedvalue": "CALCULATION_VALUE",
    "calculation_value": "CALCULATION_VALUE",
    "calculatedvaluetype": "CALCULATED_VALUE_TYPE",
    "calculated_value_type": "CALCULATED_VALUE_TYPE",
    "displaydate": "DISPLAY_DATE",
    "display_date": "DISPLAY_DATE",
    "resultdate": "RESULT_DATE",
    "result_date": "RESULT_DATE",
}
_REVIEW_VALUE_TYPE_NAMES = {
    "DECIMAL": "decimal",
    "STRING": "string",
    "DATE": "date",
    "SOURCE": "source",
    "START_DATE": "start date",
    "LIST_START_DATES_W30A": "list of w30a start dates",
    "LIST_START_DATES_W30B": "list of w30b start dates",
    "COUNT_W30A": "count of w30a visits",
    "COUNT_W30B": "count of w30b visits",
    "CODE": "code",
    "LONG": "long",
}
_REVIEW_VALUE_TYPES = {n.lower() for n in _REVIEW_VALUE_TYPE_NAMES} | set(_REVIEW_VALUE_TYPE_NAMES.values())
_REVIEW_EMR_CALL = re.compile(r"\b(addPatientEMR|addEncounterEMR)\s*\(")
_REVIEW_STATUS_CALL = re.compile(
    r"\b(addPatientMeasureStatus|addProviderPatientMeasureStatus|addGroupPatientMeasureStatus|"
    r"addEncounterMeasureStatus|addOrUpdateHedisMeasureStatus)\s*\("
)
_REVIEW_RULE = re.compile(r"(?ms)^\s*rule\s+(['\"])(.+?)\1(.*?)^\s*end\b")
_REVIEW_DECLARE = re.compile(r"(?ms)^\s*declare\s+(\w+)\b(.*?)\bend\b")
_REVIEW_QUERY_DECL = re.compile(r"(?m)^\s*query\s+(\w+)\s*\(([^)]*)\)")
_REVIEW_PROVIDER_ATTRIBUTION = re.compile(
    r"addPatientEMR\s*\(\s*[^,()]+,\s*[^,()]+,\s*[^,()]+,\s*\$\w+\.(?:providerExternalId|getProviderExternalId\(\))"
)
_REVIEW_PROVIDER_DEDUP = re.compile(r"not\s*\(?\s*exists\s+\w+\s*\([^)]*providerExternalId")


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _strip_drl_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def _split_top_level_args(text: str, open_idx: int) -> list[str] | None:
    """Split the arguments of the call whose '(' is at open_idx; None if unbalanced."""
    depth = 0
    quote = False
    buf: list[str] = []
    args: list[str] = []
    i = open_idx
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = False
        elif ch == '"':
            quote = True
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            if depth > 1:
                buf.append(ch)
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                args.append("".join(buf).strip())
                return args
            buf.append(ch)
        elif ch == "," and depth == 1:
            args.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    return None


def _review_emr_calls(text: str) -> list[tuple[str, list[str]]]:
    calls: list[tuple[str, list[str]]] = []
    for m in _REVIEW_EMR_CALL.finditer(text):
        args = _split_top_level_args(text, m.end() - 1)
        if args is not None:
            calls.append((m.group(1), args))
    return calls


def _review_supp_key(arg: str) -> str | None:
    """Canonical SupplementalInfoEnum name, '' if invalid, None if not a literal."""
    m = re.fullmatch(r"SupplementalInfoEnum\.(\w+)\.displayName\(\)", arg)
    if m:
        name = m.group(1).upper()
        return name if name in _REVIEW_SUPP_KEYS.values() else ""
    m = re.fullmatch(r'"([^"]*)"', arg)
    if m:
        return _REVIEW_SUPP_KEYS.get(m.group(1).lower(), "")
    return None


def _rules_engine_review_warnings(baseline_text: str, generated_text: str) -> list[str]:
    """Warnings for new/changed DRL code that the rules-engine PR reviewer would flag."""
    gen = _strip_drl_comments(generated_text)
    base = _strip_drl_comments(baseline_text)
    pkg = re.search(r"(?m)^\s*package\s+com\.ablehealth\.measure\.(\w+)", gen)
    program = pkg.group(1).lower() if pkg else ""
    out: list[str] = []

    def add(tag: str, message: str) -> None:
        line = f"[rules-engine review {tag}] {message}"
        if line not in out:
            out.append(line)

    # A1/A2/A4/A5/A6: EMR call arguments.
    base_calls = {_norm_ws(f"{n}({','.join(a)})") for n, a in _review_emr_calls(base)}
    for name, args in _review_emr_calls(gen):
        if len(args) < 8 or _norm_ws(f"{name}({','.join(args)})") in base_calls:
            continue
        short = _norm_ws(f"{name}({', '.join(args[:6])}, ...)")[:160]
        attribution, encounterable, supp = args[3], args[7], args[8:]
        if name == "addPatientEMR":
            if attribution == "null":
                add("A1", f'Org-level EMR attribution must be "" (not null): {short}')
            elif attribution != '""' and encounterable == "null" and not any(
                _review_supp_key(k) == "CALCULATION_VALUE" for k in supp[0::2]
            ):
                add("A2", f"Group/provider EMR passes a null encounterable; pass $encounter: {short}")
        if len(supp) % 2:
            add(
                "A4",
                f"Odd supplementalInfo count ({len(supp)}) silently drops all supplemental data; "
                f"a value-type literal needs SupplementalInfoEnum.CALCULATED_VALUE_TYPE.displayName() before it: {short}",
            )
            continue
        own_objects = {encounterable} | ({attribution} if name == "addEncounterEMR" else set())
        for key, value in zip(supp[0::2], supp[1::2]):
            key_name = _review_supp_key(key)
            if key_name == "":
                add("A5", f"Invalid supplementalInfo key {key} throws at runtime: {short}")
            elif key_name == "CALCULATED_VALUE_TYPE":
                lit = re.fullmatch(r'"([^"]*)"', value)
                enum = re.fullmatch(r"ElementValueType\.(\w+)\.displayName\(\)", value)
                if (lit and lit.group(1).lower() not in _REVIEW_VALUE_TYPES) or (
                    enum and enum.group(1).upper() not in _REVIEW_VALUE_TYPE_NAMES
                ):
                    add("A5", f"Invalid CALCULATED_VALUE_TYPE {value} throws at runtime; use Long or Decimal: {short}")
            elif key_name in ("DISPLAY_DATE", "RESULT_DATE"):
                obj = re.fullmatch(r"(\$?\w+)\.toString\(\)", value)
                if obj and obj.group(1) in own_objects:
                    add("A6", f"{key_name} value {value} is an object toString(), not a date; use {obj.group(1)}.startDate.toString(): {short}")

    # A3/B1/C1: changed rules.
    query_params = {
        m.group(1): len([p for p in m.group(2).split(",") if p.strip()]) for m in _REVIEW_QUERY_DECL.finditer(gen)
    }
    base_rules = {_norm_ws(m.group(0)) for m in _REVIEW_RULE.finditer(base)}
    for m in _REVIEW_RULE.finditer(gen):
        if _norm_ws(m.group(0)) in base_rules:
            continue
        rule_name, body = m.group(2), m.group(3)
        has_emr = bool(_REVIEW_EMR_CALL.search(body))
        has_status = bool(_REVIEW_STATUS_CALL.search(body))
        negative = bool(re.search(r"\bsalience\s+-\s*\d+", body))
        if program in ("mips", "cms") and has_emr:
            if has_status and negative:
                add("B1", f"Rule '{rule_name}' sets a status and has negative salience; status rules keep default salience")
            elif not has_status and not negative:
                add("B1", f"Pure EMR rule '{rule_name}' needs negative salience (salience -10)")
        if (
            _REVIEW_PROVIDER_ATTRIBUTION.search(body)
            and _REVIEW_PROVIDER_DEDUP.search(body)
            and "isPresent(" not in body
        ):
            add("A3", f"Rule '{rule_name}' writes provider-deduped EMR without an isPresent(providerExternalId) guard")
        for query_name, expected in query_params.items():
            for call in re.finditer(rf"\b{re.escape(query_name)}\s*\(([^;()]*);", body):
                passed = len([a for a in call.group(1).split(",") if a.strip()])
                if passed != expected:
                    add(
                        "C1",
                        f"Rule '{rule_name}' calls {query_name} with {passed} parameter(s) but it declares {expected}; "
                        "the exists silently evaluates false",
                    )

    # B2: provider/group EMR markers must be keyed.
    base_decls = {_norm_ws(m.group(0)) for m in _REVIEW_DECLARE.finditer(base)}
    for m in _REVIEW_DECLARE.finditer(gen):
        if _norm_ws(m.group(0)) in base_decls or not re.search(r"emr", m.group(1), re.IGNORECASE):
            continue
        decl_name, decl_body = m.group(1), m.group(2)
        if re.search(r"provider", decl_name, re.IGNORECASE) and "providerExternalId" not in decl_body:
            add("B2", f"Marker {decl_name} needs a providerExternalId: String field, or it acts as a session-wide singleton")
        elif re.search(r"group", decl_name, re.IGNORECASE) and "groupExternalId" not in decl_body:
            add("B2", f"Marker {decl_name} needs a groupExternalId: String field, or it acts as a session-wide singleton")

    # D4: raw date durations on new lines.
    base_lines = {_norm_ws(line) for line in base.splitlines()}
    for line in gen.splitlines():
        norm = _norm_ws(line)
        if norm and norm not in base_lines and re.search(r"ChronoUnit\.DAYS\.between\s*\(|\.datesUntil\s*\(", norm):
            add(
                "D4",
                "Raw date duration can go negative or throw for reversed intervals; prove end >= start in the when "
                f"clause or use DateHelper.retrieveDatesFromRange (DateHelper.daysBetween does not exist): {norm[:140]}",
            )

    if len(out) > 25:
        out = out[:25] + [f"[rules-engine review] {len(out) - 25} more finding(s) not shown"]
    return out


@mcp.tool()
def check_drl_already_updated(ticket_id: str = "") -> dict[str, Any]:
    """Step 2: decide whether the local rules-engine DRL already contains the PR's CQL change.

    For each intake CQL it reads the Step 1 patch (artifacts/<ticket>/patches/<slug>-cql-diff.patch),
    locates the matching DRL in the local rules-engine working copy, and checks whether any
    distinctive added (green) CQL line already appears (whitespace-normalized) inside the DRL —
    each DRL rule carries its CQL as a /* define ... */ comment block, so a present change means
    the DRL is already updated.

    Decision rules (fail toward conversion — never wrongly skip needed work):
    - A measure counts as updated only when its DRL exists AND a distinctive green line is found.
    - Missing DRL, missing patch, or no distinctive green lines => not updated (convert).
    - Overall alreadyUpdated is True only when >=1 measure was evaluated and ALL are updated.

    When overall alreadyUpdated is True this writes the terminal pipeline stage
    'already-implemented'; the caller should then stop and report. Otherwise the caller
    proceeds with the normal conversion flow.

    Args:
        ticket_id: Optional ticket id. If omitted, inferred from run-input.json.
    """
    try:
        ticket = _resolve_ticket_id(ticket_id)
        intake_path = ROOT_DIR / "artifacts" / ticket / "intake.json"
        if not intake_path.exists():
            return {"ok": False, "error": f"Missing intake artifact: {intake_path}"}
        intake = _load_json_file(intake_path)

        cql_paths = [str(p).strip() for p in (intake.get("cqlPaths") or []) if str(p).strip()]
        if not cql_paths:
            return {"ok": False, "error": "intake.cqlPaths is empty"}

        # baseStatus per slug from Step 1: 'edited'/'added' diffs are trustworthy,
        # 'base-unavailable' means the diff could not be computed against a real base.
        base_status_by_slug: dict[str, str] = {}
        extraction_path = ROOT_DIR / "artifacts" / ticket / "extraction.json"
        if extraction_path.exists():
            try:
                for d in (_load_json_file(extraction_path).get("cqlPrDiffs") or []):
                    s = str(d.get("slug", "")).strip()
                    if s:
                        base_status_by_slug[s] = str(d.get("baseStatus", "")).strip()
            except Exception:
                pass

        rules_engine_root = _resolve_rules_engine_root()
        patches_dir = ROOT_DIR / "artifacts" / ticket / "patches"

        measures: list[dict[str, Any]] = []
        for cql_path in cql_paths:
            ctx = _derive_measure_context(ticket, source_cql_path=cql_path)
            slug = ctx["measureSlug"]
            family = ctx["measureFamilyLower"]
            year = ctx["currentYear"]
            drl_path = (
                rules_engine_root
                / ctx["drlTargetPath"]
                / family
                / f"year{year}"
                / f"{slug}.drl"
            )
            patch_path = patches_dir / f"{slug}-cql-diff.patch"

            record: dict[str, Any] = {
                "slug": slug,
                "cqlPath": cql_path,
                "drlPath": str(drl_path),
                "patchFile": str(patch_path),
                "drlExists": drl_path.exists(),
                "patchExists": patch_path.exists(),
                "baseStatus": base_status_by_slug.get(slug, ""),
                "alreadyUpdated": False,
                "matchedLine": "",
                "reason": "",
            }

            if not patch_path.exists():
                record["reason"] = "no-patch"
            elif not drl_path.exists():
                record["reason"] = "drl-not-found"
            elif base_status_by_slug.get(slug) == "base-unavailable":
                # Diff is the whole file (no trustworthy base) — cannot confirm the change; convert.
                record["reason"] = "base-unavailable"
            else:
                green = _distinctive_green_lines(patch_path.read_text(encoding="utf-8", errors="replace"))
                record["greenLineCount"] = len(green)
                if not green:
                    record["reason"] = "no-distinctive-green-lines"
                else:
                    drl_norm = re.sub(r"\s+", " ", drl_path.read_text(encoding="utf-8", errors="replace"))
                    matched = next((g for g in green if g in drl_norm), "")
                    if matched:
                        record["alreadyUpdated"] = True
                        record["matchedLine"] = matched
                        record["reason"] = "matched"
                    else:
                        record["reason"] = "no-match"
            measures.append(record)

        overall = bool(measures) and all(m["alreadyUpdated"] for m in measures)

        stage_written = ""
        if overall:
            TicketStateManager.write_pipeline_status(
                ticket,
                PipelineStage.ALREADY_IMPLEMENTED,
                "DRL already contains the PR CQL change; conversion skipped.",
            )
            stage_written = PipelineStage.ALREADY_IMPLEMENTED.value

        return {
            "ok": True,
            "ticketId": ticket,
            "alreadyUpdated": overall,
            "recommendedAction": "stop-already-implemented" if overall else "proceed-with-conversion",
            "stageWritten": stage_written,
            "rulesEngineRoot": str(rules_engine_root),
            "measures": measures,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def validate_conversion_artifacts(ticket_id: str) -> dict[str, Any]:
    """Run conversion artifact validation for a ticket.

    Args:
        ticket_id: Required ticket id for validation target.
    """
    ticket = ticket_id.strip()
    if not ticket or not ticket.isdigit() or len(ticket) > 20:
        return {
            "ok": False,
            "returnCode": 2,
            "command": "",
            "stdout": "",
            "stderr": "ticket_id is required and must be a numeric ADO id",
        }

    return _run_python_script(
        ["scripts/validate_conversion_artifacts.py", "--ticket", ticket]
    )


@mcp.tool()
def validate_drl_changes(ticket_id: str, measure_slug: str) -> dict[str, Any]:
    """Validate the generated DRL for a measure: checks patch is non-empty, no legacy EMR API, no forbidden patterns.

    Args:
        ticket_id: Required ticket id.
        measure_slug: Measure slug (e.g., trca, trcshared, cms22).
    """
    ticket = _resolve_ticket_id(ticket_id)
    slug = str(measure_slug or "").strip().lower()
    if not ticket:
        return {"ok": False, "error": "ticket_id is required"}
    if not slug:
        return {"ok": False, "error": "measure_slug is required"}
    if not _is_safe_slug(slug):
        return {"ok": False, "error": f"Invalid measure_slug: {slug!r}"}

    artifacts_root = ROOT_DIR / "artifacts" / ticket

    generated_path = _generated_drl_path(artifacts_root / "drls" / "generated", slug)
    baseline_path = artifacts_root / "drls" / "baseline" / f"{slug}.drl"
    patch_path = artifacts_root / "patches" / f"{ticket}-{slug}-drl.patch"

    issues: list[str] = []
    warnings: list[str] = []

    # Check files exist
    if not generated_path.exists():
        return {"ok": False, "error": f"Generated DRL not found: {generated_path.name}"}
    if not baseline_path.exists():
        return {"ok": False, "error": f"Baseline DRL not found: {baseline_path.name}"}

    generated_text = generated_path.read_text(encoding="utf-8", errors="replace")
    baseline_text = baseline_path.read_text(encoding="utf-8", errors="replace")

    # Verify generated is not empty / stub / truncated relative to baseline
    gen_lines = [l for l in generated_text.splitlines() if l.strip() and not l.strip().startswith("//")]
    base_lines = [l for l in baseline_text.splitlines() if l.strip() and not l.strip().startswith("//")]
    if len(gen_lines) < 5:
        issues.append("Generated DRL appears to be a stub or nearly empty — baseline copy was not modified")
    elif len(base_lines) >= 20 and len(gen_lines) < len(base_lines) * 0.6:
        issues.append(
            f"Generated DRL is far smaller than baseline ({len(gen_lines)} vs {len(base_lines)} code lines) — "
            "baseline content was likely dropped or summarized. Re-run initialize_generated_drls and apply targeted edits."
        )
    if any(
        m in generated_text.lower()
        for m in ("omitted for brevity", "content omitted", "simplified placeholder", "kept baseline content omitted")
    ):
        issues.append("Generated DRL contains placeholder/omission text — it must be the full DRL, not a summary.")

    # Structural integrity vs baseline. Runs for ANY write path — including direct `edit`-tool
    # changes that bypass write_generated_drl — so dropped rules, truncation (missing trailing
    # `end`, unbalanced braces/parens), undersized non-blank content, and double-spacing are
    # caught here even when the lenient line-count heuristic above passes.
    integrity_ok, integrity_error = _validate_generated_drl_integrity(baseline_text, generated_text)
    if not integrity_ok:
        issues.append(integrity_error)

    # Check for legacy EMR API patterns (2025-style, should be replaced in 2026)
    _LEGACY_EMR = re.compile(r"\baddExtendedMeasureResults\s*\(")
    if _LEGACY_EMR.search(generated_text):
        issues.append("Legacy EMR API detected: addExtendedMeasureResults — replace with per-attribution addPatientEMR(Org/Group/Provider) rules")

    # Check for local maxCoverageDate initialization (anti-pattern)
    _LOCAL_MAP_INIT = re.compile(r"new\s+java\.util\.HashMap\s*<[^>]*>\s*\(\s*\).*maxCoverage", re.DOTALL)
    _LOCAL_MAX_STREAM = re.compile(r"maxCoverage\w*\s*=\s*[^(]*\.stream\(\)")
    if _LOCAL_MAP_INIT.search(generated_text) or _LOCAL_MAX_STREAM.search(generated_text):
        issues.append("maxCoverageDateByPayer must be a Drools global (declared at file top), not computed locally — remove local HashMap init and use 'global Map<String, LocalDate> maxCoverageDateByPayer' instead. Measure must also be added to MapperUseEnrollmentsRules.json.")

    # Check for legacy groupExternalId
    _LEGACY_GROUP = re.compile(r"\.groupExternalId\b|getGroupExternalId\s*\(")
    if _LEGACY_GROUP.search(generated_text):
        warnings.append("Legacy groupExternalId usage detected — verify migration to current group ID pattern")

    # CE: maxCoverageDateByPayer global declaration check
    _MAX_COV_USED = re.compile(r"\bmaxCoverageDateByPayer\b")
    _MAX_COV_GLOBAL = re.compile(r"^global\s+Map.*\bmaxCoverageDateByPayer\b", re.MULTILINE)
    if _MAX_COV_USED.search(generated_text) and not _MAX_COV_GLOBAL.search(generated_text):
        issues.append(
            "maxCoverageDateByPayer is referenced but not declared as a Drools global — "
            "add 'global Map<String, LocalDate> maxCoverageDateByPayer;' at the file top "
            "and register the measure in MapperUseEnrollmentsRules.json."
        )

    # CE max-coverage signature/import/anti-pattern checks.
    # A measure is "max-coverage" if it reads the global or its CQL comment references GetMaxCoverageEndDateForPayer.
    _CQL_COMMENT_MAX_COV = re.compile(r"/\*[^*]*GetMaxCoverageEndDateForPayer", re.DOTALL)
    uses_max_coverage = bool(_MAX_COV_USED.search(generated_text)) or bool(
        _CQL_COMMENT_MAX_COV.search(generated_text)
    )
    if uses_max_coverage:
        # Every addContinuousEnrollment call must pass coverageEndDate. The plain 6-arg (patient)
        # / 7-arg (encounter) overloads silently DROP the per-payer max coverage date.
        ce_calls = re.findall(r"addContinuousEnrollment\s*\(([^;]*?)\)", generated_text, re.DOTALL)
        if ce_calls and any("coverageEndDate" not in c for c in ce_calls):
            issues.append(
                "Max-coverage measure: addContinuousEnrollment is called without the coverageEndDate "
                "argument, so the per-payer max coverage date is dropped. Use the max-coverage overload — "
                "patient: addContinuousEnrollment($program, $patient, $measure, payer, \"\", coverageEndDate, hasCE, optionId); "
                "encounter: addContinuousEnrollment($program, $patient, $encounter, $measure, payer, \"\", coverageEndDate, hasCE, optionId)."
            )
        # Required imports — missing import => compile failure.
        if "getEarliestDate(" in generated_text and not re.search(
            r"import\s+static\s+com\.ablehealth\.helper\.DateHelper\.getEarliestDate\s*;", generated_text
        ):
            issues.append(
                "getEarliestDate(...) is used but its static import is missing — add "
                "'import static com.ablehealth.helper.DateHelper.getEarliestDate;'."
            )
        if re.search(r"\bLocalDate\b", generated_text) and not re.search(
            r"import\s+java\.time\.LocalDate\s*;|import\s+java\.time\.\*\s*;", generated_text
        ):
            issues.append("LocalDate is used but not imported — add 'import java.time.LocalDate;'.")
        # Non-deterministic interval driver — reference DRLs use program/encounter dates, never now().
        if re.search(r"\bLocalDate\.now\s*\(", generated_text):
            issues.append(
                "LocalDate.now() must not drive continuous-enrollment intervals — the result becomes "
                "run-date dependent. Use program/encounter dates (e.g. $program.measurementPeriodEndDate, "
                "$encounter.startDate.plusDays(N)) as in the WCCA/URI reference DRLs."
            )
        # Hallucinated defensive code not present in reference DRLs (the global is always injected).
        if re.search(r"maxCoverageDateByPayer\s*!=\s*null", generated_text) or re.search(
            r"catch\s*\(\s*Exception", generated_text
        ):
            issues.append(
                "Defensive null-check / try-catch around maxCoverageDateByPayer is not used in reference "
                "DRLs — the global is always injected by the framework. Read it directly: "
                "LocalDate coverageEndDate = (LocalDate) maxCoverageDateByPayer.get(payer);"
            )
        has_explicit_max_guard = bool(
            re.search(r"if\s+[^\n]+\s+is\s+after\s+\"GetMaxCoverageEndDateForPayer\"", generated_text)
        )
        if has_explicit_max_guard:
            if re.search(r"coverageEndDate\s*==\s*null\s*\?", generated_text):
                issues.append(
                    "CQL has an explicit 'is after GetMaxCoverageEndDateForPayer(P) return false' guard, "
                    "but the DRL uses a nullable fallback for coverageEndDate. Preserve the false branch as "
                    "coverageEndDate != null && !visitDate.isAfter(coverageEndDate) before isContinuous(...)."
                )
            # if/else null-fallback: if (coverageEndDate != null) { ... } else { evaluate CE without cap }
            # This incorrectly treats absent coverage as eligible. The CQL guard means null => false.
            if re.search(
                r"if\s*\(\s*coverageEndDate\s*!=\s*null\s*\)[\s\S]{0,600}}\s*else\s*\{",
                generated_text,
            ):
                issues.append(
                    "CQL has an explicit 'is after GetMaxCoverageEndDateForPayer(P) return false' guard, "
                    "but the DRL uses an if/else block that evaluates CE in the else branch when coverageEndDate "
                    "is null. A null coverage date must produce hasContinuousEnrollment = false, not a "
                    "fallback evaluation. Use the short-circuit && chain: "
                    "coverageEndDate != null && !<start>.isAfter(coverageEndDate) && isContinuous(...) — "
                    "no else branch."
                )
            if not re.search(r"coverageEndDate\s*!=\s*null[\s\S]{0,180}!\s*[^;\n]+\.isAfter\(\s*coverageEndDate\s*\)", generated_text):
                issues.append(
                    "CQL has an explicit max-coverage start-date guard, but the DRL does not show the "
                    "required short-circuit pattern: coverageEndDate != null && !<start>.isAfter(coverageEndDate)."
                )
        # Registration is automatic: publish_approved_pr_files adds the MapperUseEnrollmentsRules entry.
        if _MAX_COV_USED.search(generated_text):
            try:
                ctx = _derive_measure_context(ticket, measure_slug=slug)
                mapper_path = _resolve_rules_engine_root() / _ENROLLMENT_MAPPER_PATH
                target_name = _baseline_target_file_name(
                    _safe_json_load(artifacts_root / "baseline" / f"{slug}-baseline.json") or {}, slug
                )
                rel_key = f"measure/{ctx['measureFamilyLower']}/year{ctx['currentYear']}/{target_name}"
                if mapper_path.exists() and rel_key not in mapper_path.read_text(
                    encoding="utf-8", errors="replace"
                ):
                    warnings.append(
                        f"{rel_key} is not yet in MapperUseEnrollmentsRules.json; publish_approved_pr_files "
                        "registers it in the same PR commit. Do not edit the mapper by hand."
                    )
            except Exception:
                pass

    # CE option fan-out must include all concrete CQL option IDs. The converter previously
    # copied only EMR-bearing options and omitted exclusions such as denominator_exclusion_deceased.
    if "addContinuousEnrollment(" in generated_text:
        cql_option_ids = set(re.findall(r"Option\s+ID:\s*([A-Za-z0-9_\-]+)", generated_text))
        ce_option_arrays = re.findall(r"String\[\]\s+optionIds\s*=\s*new\s+String\[\]\s*\{([^}]*)\}", generated_text)
        if cql_option_ids and ce_option_arrays:
            ce_option_ids = set()
            for option_array in ce_option_arrays:
                ce_option_ids.update(re.findall(r'"([A-Za-z0-9_\-]+)"', option_array))
            missing_ce_options = sorted(cql_option_ids - ce_option_ids)
            if missing_ce_options:
                issues.append(
                    "Continuous enrollment optionIds omit concrete CQL option ID(s): "
                    f"{', '.join(missing_ce_options)}. Build CE fan-out from all /* Option ID: ... */ "
                    "entries, including exclusions that do not emit EMR records."
                )

    # CE: when CQL comment contains HasEnrollmentOnDateByBenefitTypes with a benefit set that
    # differs from the interval, a separate medical-only collect list must be present.
    # Heuristic: CQL comment has 'HasEnrollmentOnDateByBenefitTypes' with {'medical'} (no 'pharmacy')
    # while the interval uses {'medical' and 'pharmacy'}.
    _CQL_ANCHOR_MED_ONLY = re.compile(
        r"/\*.*HasEnrollmentOnDateByBenefitTypes.*\{'medical'\}.*\*/",
        re.DOTALL,
    )
    _CQL_INTERVAL_MED_PHARM = re.compile(
        r"/\*.*HasContinuousEnrollmentByBenefitTypes.*'medical'\s+and\s+'pharmacy'.*\*/",
        re.DOTALL,
    )
    _MEDICAL_ONLY_COLLECT = re.compile(
        r"from\s+collect\s*\([^)]*hasMedicalCoverage\s*==\s*true[^)]*\)"
    )
    _MEDICAL_PHARM_COLLECT = re.compile(
        r"from\s+collect\s*\([^)]*hasMedicalCoverage\s*==\s*true[^)]*hasPharmacyCoverage\s*==\s*true"
    )
    if (
        _CQL_ANCHOR_MED_ONLY.search(generated_text)
        and _CQL_INTERVAL_MED_PHARM.search(generated_text)
    ):
        has_combined = bool(_MEDICAL_PHARM_COLLECT.search(generated_text))
        has_medical_only = bool(_MEDICAL_ONLY_COLLECT.search(generated_text))
        if has_combined and not has_medical_only:
            issues.append(
                "CQL anchor-date check uses {'medical'} but only a combined medical+pharmacy collect list "
                "exists in the DRL. Add a separate medical-only enrollment list for the "
                "HasEnrollmentOnDateByBenefitTypes check (see SAA 2026 pattern with medicalEnrollmentList)."
            )

    # CQL comment freshness: the /* */ CQL comment blocks embedded in the DRL must reflect the
    # NEW CQL from the PR, not the baseline. The converter updates rule logic but can forget to
    # replace the commented CQL. If a line the PR REMOVED from the CQL still appears inside a DRL
    # comment block, the embedded CQL is stale. Only comment regions are inspected, so DRL Java
    # logic can never cause a false positive.
    try:
        patches_dir = artifacts_root / "patches"
        removed_cql: set[str] = set()
        added_cql: set[str] = set()
        for patch_file in sorted(patches_dir.glob("*-cql-diff.patch")):
            ptext = patch_file.read_text(encoding="utf-8", errors="replace")
            removed_cql.update(_distinctive_diff_lines(ptext, "-"))
            added_cql.update(_distinctive_diff_lines(ptext, "+"))
        truly_removed = [ln for ln in removed_cql if ln not in added_cql]
        if truly_removed:
            comment_norm = re.sub(
                r"\s+", " ", " ".join(re.findall(r"/\*.*?\*/", generated_text, re.DOTALL))
            )
            stale = [ln for ln in truly_removed if ln in comment_norm]
            if stale:
                sample = "; ".join(f'"{s}"' for s in stale[:2])
                issues.append(
                    "Embedded CQL comment is stale: line(s) the PR removed from the CQL still appear in "
                    f"the DRL /* */ comment (e.g. {sample}). Replace the baseline CQL comment block with the "
                    "new CQL from the diff so the commented CQL matches the converted rule logic."
                )
    except Exception:
        pass

    # Advisory only: never affects ok, so existing conversions keep passing.
    try:
        warnings.extend(_rules_engine_review_warnings(baseline_text, generated_text))
    except Exception:
        pass

    # Patch presence check
    if patch_path.exists():
        patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
        hunk_count = patch_text.count("\n@@")
        if hunk_count == 0 and generated_text.strip() != baseline_text.strip():
            warnings.append("Patch file exists but has no hunks, yet generated differs from baseline — re-run generate_drl_patch")
    else:
        warnings.append(f"Patch file not yet generated: {patch_path.name}")

    ok = len(issues) == 0
    return {
        "ok": ok,
        "ticketId": ticket,
        "measureSlug": slug,
        "issues": issues,
        "warnings": warnings,
        "generatedLines": len(generated_text.splitlines()),
        "baselineLines": len(baseline_text.splitlines()),
        "message": "Validation passed" if ok else f"{len(issues)} issue(s) found",
    }


def _canonical_unified_diff_text(
    *,
    root: Path,
    baseline_path: Path,
    generated_path: Path,
) -> str:
    baseline_text = baseline_path.read_text(encoding="utf-8", errors="replace")
    generated_text = generated_path.read_text(encoding="utf-8", errors="replace")
    return "".join(
        difflib.unified_diff(
            baseline_text.splitlines(keepends=True),
            generated_text.splitlines(keepends=True),
            fromfile=str(baseline_path.relative_to(root)).replace("\\", "/"),
            tofile=_generated_drl_label(root, generated_path),
        )
    )


def _changed_diff_lines(patch_text: str) -> list[str]:
    """Extract the added/removed content lines from a unified diff (ignoring file/hunk headers)."""
    changed: list[str] = []
    for line in (patch_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            changed.append(line.rstrip())
    return changed


def _diff_matches_approved(recomputed_patch: str, approved_patch: str) -> bool:
    """True when a freshly recomputed baseline->generated diff matches the approved patch.

    Compares the set of added/removed content lines so that benign context-line or header-path
    drift does not cause a false mismatch, while any real change to what ships is caught.
    """
    norm_recomputed = (recomputed_patch or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    norm_approved = (approved_patch or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if norm_recomputed == norm_approved:
        return True
    return _changed_diff_lines(recomputed_patch) == _changed_diff_lines(approved_patch)


@mcp.tool()
def generate_drl_patch(ticket_id: str, measure_slug: str) -> dict[str, Any]:
    """Generate canonical full unified DRL patch baseline->generated for one measure.

    Args:
        ticket_id: Ticket id containing artifacts.
        measure_slug: Canonical measure slug (for example: cms22, mips440).
    """
    ticket = _resolve_ticket_id(ticket_id)
    slug = str(measure_slug or "").strip().lower()
    if not ticket:
        return {"ok": False, "error": "ticket_id is required"}
    if not slug:
        return {"ok": False, "error": "measure_slug is required"}
    if not _is_safe_slug(slug):
        return {"ok": False, "error": f"Invalid measure_slug: {slug!r}"}

    baseline_path = ROOT_DIR / "artifacts" / ticket / "drls" / "baseline" / f"{slug}.drl"
    generated_path = _generated_drl_path(ROOT_DIR / "artifacts" / ticket / "drls" / "generated", slug)
    patch_path = ROOT_DIR / "artifacts" / ticket / "patches" / f"{ticket}-{slug}-drl.patch"

    missing: list[str] = []
    if not baseline_path.exists():
        missing.append(str(baseline_path.relative_to(ROOT_DIR)).replace("\\", "/"))
    if not generated_path.exists():
        missing.append(str(generated_path.relative_to(ROOT_DIR)).replace("\\", "/"))
    if missing:
        return {
            "ok": False,
            "error": "Missing required DRL input(s) for patch generation",
            "missing": missing,
        }

    diff_text = _canonical_unified_diff_text(
        root=ROOT_DIR,
        baseline_path=baseline_path,
        generated_path=generated_path,
    )

    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(diff_text, encoding="utf-8")

    lines = diff_text.splitlines()
    hunk_headers = [line for line in lines if line.startswith("@@")]
    baseline_meta = _safe_json_load(ROOT_DIR / "artifacts" / ticket / "baseline" / f"{slug}-baseline.json")
    return {
        "ok": True,
        "ticketId": ticket,
        "measureSlug": slug,
        "targetFileName": _baseline_target_file_name(baseline_meta if isinstance(baseline_meta, dict) else {}, slug),
        "patchPath": str(patch_path.relative_to(ROOT_DIR)).replace("\\", "/"),
        "lineCount": len(lines),
        "hunkCount": len(hunk_headers),
        "firstHunks": hunk_headers[:3],
    }


@mcp.tool()
def read_baseline_drl(ticket_id: str, measure_slug: str) -> dict[str, Any]:
    """Read the full content of a baseline DRL file.

    Use this to get the baseline DRL content before applying changes in write_generated_drl.

    Args:
        ticket_id: Ticket id containing artifacts.
        measure_slug: Measure slug matching the DRL filename (e.g., "trcshared", "trca").
    """
    try:
        ticket = _resolve_ticket_id(ticket_id)
        slug = str(measure_slug or "").strip().lower()
        if not ticket:
            return {"ok": False, "error": "ticket_id is required"}
        if not slug:
            return {"ok": False, "error": "measure_slug is required"}
        if not _is_safe_slug(slug):
            return {"ok": False, "error": f"Invalid measure_slug: {slug!r}"}

        baseline_path = ROOT_DIR / "artifacts" / ticket / "drls" / "baseline" / f"{slug}.drl"
        if not baseline_path.exists():
            return {
                "ok": False,
                "error": f"Baseline DRL not found: {baseline_path.name}. Available slugs: "
                + ", ".join(p.stem for p in (baseline_path.parent).glob("*.drl")),
            }

        content = baseline_path.read_text(encoding="utf-8")
        return {
            "ok": True,
            "ticketId": ticket,
            "measureSlug": slug,
            "content": content,
            "lineCount": len(content.splitlines()),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _validate_generated_drl_integrity(baseline_text: str, content: str) -> tuple[bool, str]:
    """Structurally validate a full-file generated DRL against its baseline.

    Full-file rewrites by an LLM are the primary corruption vector: a small model truncates
    the file mid-rule, silently drops whole rules, or double-spaces every line. Raw line-count
    checks are fooled by blank-line padding (double-spacing can push the count *above* the
    baseline while half the real content is gone), so this validates structure and non-blank
    content instead. Returns (ok, error_message); error_message is '' when ok.
    """
    base = baseline_text or ""
    gen = content or ""

    # 1) Rule preservation. A conversion edits rule bodies; it virtually never deletes whole
    #    rules. Any baseline `rule '<name>'` missing from the rewrite means content was lost
    #    (this is exactly how the ContinuousEnrollment rule vanished from trcshared.drl).
    rule_name_re = re.compile(r"(?m)^\s*rule\s+(['\"])(.+?)\1")
    base_rules = [m.group(2) for m in rule_name_re.finditer(base)]
    gen_rule_set = {m.group(2) for m in rule_name_re.finditer(gen)}
    missing_rules = [r for r in base_rules if r not in gen_rule_set]
    if missing_rules:
        shown = ", ".join(repr(r) for r in missing_rules[:5]) + (" ..." if len(missing_rules) > 5 else "")
        return False, (
            f"Refusing to write generated DRL: {len(missing_rules)} baseline rule(s) are missing "
            f"from the content: {shown}. A full-file rewrite dropped rules (truncation/omission). "
            "Re-run initialize_generated_drls and make a targeted edit (the `edit` tool) to only "
            "the rule(s) the CQL change touches; never rewrite the whole file from scratch."
        )

    # 2) Truncation. Every DRL rule/declare block ends with a line `end`; if the baseline ends
    #    with `end` the rewrite must too, otherwise it was cut off mid-block.
    def _last_nonblank(text: str) -> str:
        for ln in reversed(text.splitlines()):
            if ln.strip():
                return ln.strip()
        return ""

    if _last_nonblank(base) == "end" and _last_nonblank(gen) != "end":
        return False, (
            "Refusing to write generated DRL: the baseline ends with `end` but the content ends "
            f"with {_last_nonblank(gen)!r}. The file was truncated mid-rule. Write the complete "
            "file or use a targeted edit."
        )

    # 3) Bracket balance (only enforced for symbols the baseline itself balances, so comment
    #    text with stray brackets cannot cause false positives). Truncated then-blocks leave
    #    `{`/`}` or `(`/`)` unbalanced.
    for sym_open, sym_close, name in (("{", "}", "braces"), ("(", ")", "parentheses")):
        if base.count(sym_open) == base.count(sym_close) and gen.count(sym_open) != gen.count(sym_close):
            return False, (
                f"Refusing to write generated DRL: unbalanced {name} "
                f"({gen.count(sym_open)} '{sym_open}' vs {gen.count(sym_close)} '{sym_close}'); "
                "baseline is balanced. The content was truncated. Use a targeted edit."
            )

    # 4) Non-blank content size. Compare meaningful lines so blank-line padding cannot disguise
    #    lost content (the failure mode that slipped past the old raw line-count guard).
    base_nonblank = sum(1 for ln in base.splitlines() if ln.strip())
    gen_nonblank = sum(1 for ln in gen.splitlines() if ln.strip())
    if base_nonblank >= 20 and gen_nonblank < base_nonblank * 0.9:
        return False, (
            f"Refusing to write undersized DRL: {gen_nonblank} non-blank lines vs baseline "
            f"{base_nonblank}. A CE/year conversion adds lines, it does not drop ~10%+ of them. "
            "Content was lost — use a targeted edit to drls/generated/<slug>.drl instead."
        )

    # 5) Double-spacing / blank-line inflation. A corrupted writer inserts a blank line after
    #    every line; flag an abnormal jump in blank-line ratio vs the baseline.
    base_lines = base.splitlines()
    gen_lines = gen.splitlines()
    base_ratio = sum(1 for ln in base_lines if not ln.strip()) / max(len(base_lines), 1)
    gen_ratio = sum(1 for ln in gen_lines if not ln.strip()) / max(len(gen_lines), 1)
    if gen_ratio > 0.35 and gen_ratio > base_ratio + 0.2:
        return False, (
            f"Refusing to write generated DRL: blank-line ratio {gen_ratio:.0%} is abnormally high "
            f"vs baseline {base_ratio:.0%} (the file appears double-spaced). Write the DRL with the "
            "baseline's line spacing, or use a targeted edit instead of a full rewrite."
        )

    return True, ""


@mcp.tool()
def write_generated_drl(ticket_id: str, measure_slug: str, content: str) -> dict[str, Any]:
    """Write the full content of a generated DRL file.

    Use this after initialize_generated_drls to apply CQL-driven changes to a specific
    generated DRL. Pass the complete updated DRL text as content.

    Args:
        ticket_id: Ticket id containing artifacts.
        measure_slug: Measure slug matching the DRL filename (e.g., "trcshared", "trca").
        content: Full DRL file content to write (must be complete, not a diff/patch).
    """
    try:
        ticket = _resolve_ticket_id(ticket_id)
        slug = str(measure_slug or "").strip().lower()
        if not ticket:
            return {"ok": False, "error": "ticket_id is required"}
        if not slug:
            return {"ok": False, "error": "measure_slug is required"}
        if not _is_safe_slug(slug):
            return {"ok": False, "error": f"Invalid measure_slug: {slug!r}"}
        if not str(content or "").strip():
            return {"ok": False, "error": "content must not be empty"}

        generated_dir = ROOT_DIR / "artifacts" / ticket / "drls" / "generated"
        generated_dir.mkdir(parents=True, exist_ok=True)
        out_path = _generated_drl_path(generated_dir, slug)

        baseline_path = ROOT_DIR / "artifacts" / ticket / "drls" / "baseline" / f"{slug}.drl"
        if not baseline_path.exists():
            return {
                "ok": False,
                "error": f"No baseline DRL found for slug '{slug}'. Run build_prior_year_measure_template first.",
            }

        # Fail closed on truncated/summarized content. A generated DRL must contain the full
        # baseline plus edits, never a placeholder summary. This is the converter analogue of
        # the source-intake fabrication guard.
        lowered = content.lower()
        truncation_markers = (
            "omitted for brevity",
            "kept baseline content omitted",
            "content omitted",
            "truncated for brevity",
            "simplified placeholder",
            "rest of baseline",
            "unchanged baseline rules",
        )
        marker_hit = next((m for m in truncation_markers if m in lowered), "")
        if marker_hit:
            return {
                "ok": False,
                "error": (
                    f"Refusing to write truncated DRL: found placeholder text '{marker_hit}'. "
                    "Write the complete DRL (full baseline content plus your edits), never a summary. "
                    "Prefer targeted edits to drls/generated/<slug>.drl over full-file rewrites."
                ),
            }
        baseline_text = baseline_path.read_text(encoding="utf-8", errors="replace")
        integrity_ok, integrity_error = _validate_generated_drl_integrity(baseline_text, content)
        if not integrity_ok:
            return {"ok": False, "error": integrity_error}

        out_path.write_text(content, encoding="utf-8")
        return {
            "ok": True,
            "ticketId": ticket,
            "measureSlug": slug,
            "path": str(out_path.relative_to(ROOT_DIR)).replace("\\", "/"),
            "lineCount": len(content.splitlines()),
            "message": f"Written {len(content.splitlines())} lines to {out_path.name}. Call generate_drl_patch next.",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def initialize_generated_drls(ticket_id: str = "", only_missing: bool = False) -> dict[str, Any]:
    """Copy all baseline DRLs to the generated directory as the starting point for conversion.

    This MUST be called before the converter modifies any generated DRL. It ensures:
    - Every baseline DRL has a corresponding generated DRL (identical copy)
    - Individual measures that don't need changes will produce a clean/empty patch
    - The converter only needs to modify the specific generated DRL(s) that changed

    By default every generated DRL is reset to its baseline (overwriting edits). Pass
    only_missing=True to copy only baselines that have no generated counterpart yet, preserving
    already-edited generated DRLs (for example after adding a new shared baseline mid-conversion).

    Args:
        ticket_id: Ticket id. If omitted, resolves from run-input.json.
        only_missing: When true, never overwrite an existing generated DRL.
    """
    try:
        ticket = _resolve_ticket_id(ticket_id)
        if not ticket:
            return {"ok": False, "error": "ticket_id is required"}

        baseline_dir = ROOT_DIR / "artifacts" / ticket / "drls" / "baseline"
        generated_dir = ROOT_DIR / "artifacts" / ticket / "drls" / "generated"

        if not baseline_dir.exists():
            return {
                "ok": False,
                "error": f"Baseline directory not found: {baseline_dir}",
                "hint": "Run build_prior_year_measure_template and build_shared_drl_baseline first.",
            }

        generated_dir.mkdir(parents=True, exist_ok=True)

        import shutil

        # Default: always reset generated DRLs to an exact baseline copy. Overwriting (rather than
        # skipping existing files) guarantees a stale stub from a prior botched run cannot
        # survive, so every conversion starts from the full real baseline.
        reset: list[str] = []
        kept: list[str] = []
        for baseline_file in sorted(baseline_dir.glob("*.drl")):
            existing = _find_generated_drl(generated_dir, baseline_file.stem)
            dest = existing if existing is not None else generated_dir / baseline_file.name
            if only_missing and existing is not None:
                kept.append(dest.name)
                continue
            shutil.copy2(baseline_file, dest)
            reset.append(dest.name)

        if only_missing:
            message = (
                f"Copied {len(reset)} missing generated DRL(s); kept {len(kept)} existing generated DRL(s) unchanged."
            )
        else:
            message = (
                f"Reset {len(reset)} generated DRL(s) to an exact baseline copy. "
                "Apply CQL-driven changes with targeted edits to drls/generated/<slug>.drl; "
                "never summarize or omit baseline content."
            )
        return {
            "ok": True,
            "ticketId": ticket,
            "copied": reset,
            "skipped": kept,
            "onlyMissing": bool(only_missing),
            "message": message,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def organize_ticket_artifacts(ticket_id: str = "", clean: bool = False) -> dict[str, Any]:
    """Ensure ticket-first artifact directories exist under artifacts/<ticket-id>/.

    Layout created (agents write directly to these paths; no file copying):
    - artifacts/<ticket-id>/cqls/{baseline,extraction}
    - artifacts/<ticket-id>/drls/{baseline,generated}
    - artifacts/<ticket-id>/patches/
    - artifacts/<ticket-id>/baseline/
    - artifacts/<ticket-id>/pr-assets/

    Args:
        ticket_id: Optional ticket id. If omitted, resolves from run-input.json.
        clean: If true, removes existing artifacts/<ticket-id>/ before rebuilding.
    """
    try:
        ticket = _resolve_ticket_id(ticket_id)
        if not ticket:
            return {"ok": False, "error": "ticket_id is required"}

        ticket_root = ROOT_DIR / "artifacts" / ticket
        if clean and ticket_root.exists():
            _safe_remove_path(ticket_root)

        dirs = [
            ticket_root / "cqls" / "baseline",
            ticket_root / "cqls" / "extraction",
            ticket_root / "drls" / "baseline",
            ticket_root / "drls" / "generated",
            ticket_root / "patches",
            ticket_root / "baseline",
            ticket_root / "pr-assets",
        ]
        for directory in dirs:
            directory.mkdir(parents=True, exist_ok=True)

        present: list[str] = []
        for directory in dirs:
            for f in sorted(directory.iterdir()) if directory.exists() else []:
                if f.is_file():
                    present.append(str(f.relative_to(ROOT_DIR)).replace("\\", "/"))

        return {
            "ok": True,
            "ticketId": ticket,
            "root": str(ticket_root.relative_to(ROOT_DIR)).replace("\\", "/"),
            "presentFiles": present,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
        }


def _validated_ticket_root(ticket_id: str) -> tuple[str, Path]:
    ticket = _resolve_ticket_id(ticket_id)
    if not ticket or not ticket.isdigit() or len(ticket) > 20:
        raise ValueError(f"Invalid ticket id: {ticket!r}")
    return ticket, ROOT_DIR / "artifacts" / ticket


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


@mcp.tool()
def write_intake_json(intake: dict[str, Any], ticket_id: str = "") -> dict[str, Any]:
    """Write artifacts/<ticket-id>/intake.json (the only supported way for source intake to author it).

    Call after get_work_item_details and get_pr_changes, and before download_and_extract_cql.
    Validates the fields that extraction requires so a bad intake fails here, not downstream.

    Args:
        intake: Full intake object (camelCase keys). PR-linked tickets need projectUrl, non-empty
            cqlPaths, and resolvedPullRequests as a list of objects each with prId and sourceCommit.
            Explicit no-CQL tickets set cqlPaths=[] and provide measureFamily, measureSlug, measureYear.
        ticket_id: Optional ticket id. If omitted, resolves from run-input.json.
    """
    try:
        ticket, ticket_root = _validated_ticket_root(ticket_id)
        if not isinstance(intake, dict):
            return {"ok": False, "error": "intake must be a JSON object"}

        payload = dict(intake)
        declared = str(payload.get("ticketId", "")).strip()
        if declared and declared != ticket:
            return {"ok": False, "error": f"intake.ticketId {declared} does not match ticket {ticket}"}
        payload["ticketId"] = ticket

        errors: list[str] = []
        if not str(payload.get("projectUrl", "")).strip():
            errors.append("projectUrl is required")
        cql_paths = payload.get("cqlPaths", [])
        if not isinstance(cql_paths, list):
            errors.append("cqlPaths must be a list")
            cql_paths = []
        if cql_paths:
            prs = payload.get("resolvedPullRequests")
            if not isinstance(prs, list) or not prs:
                errors.append("resolvedPullRequests must be a non-empty list of PR objects when cqlPaths exist")
            else:
                for index, pr in enumerate(prs):
                    if not isinstance(pr, dict):
                        errors.append(f"resolvedPullRequests[{index}] must be an object")
                        continue
                    if not str(pr.get("prId") or pr.get("pullRequestId") or pr.get("id") or "").strip():
                        errors.append(f"resolvedPullRequests[{index}] is missing prId")
                    if not str(pr.get("sourceCommit") or pr.get("sourceCommitId") or "").strip():
                        errors.append(f"resolvedPullRequests[{index}] is missing sourceCommit")
        else:
            for key in ("measureFamily", "measureSlug", "measureYear"):
                if not str(payload.get(key, "")).strip():
                    errors.append(f"{key} is required for a no-CQL intake")
        if errors:
            return {"ok": False, "ticketId": ticket, "errors": errors}

        out_path = ticket_root / "intake.json"
        _atomic_write_text(out_path, json.dumps(payload, indent=2) + "\n")
        return {
            "ok": True,
            "ticketId": ticket,
            "path": str(out_path.relative_to(ROOT_DIR)).replace("\\", "/"),
            "cqlPathCount": len(cql_paths),
            "message": "intake.json written. Call download_and_extract_cql next.",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def write_intake_plan(content: str, ticket_id: str = "") -> dict[str, Any]:
    """Write artifacts/<ticket-id>/plan.md and initialize approval-status.json with approved=false.

    Source intake calls this after download_and_extract_cql and check_drl_already_updated succeed.
    A fresh intake always resets approval to false; prior approvals never carry over.

    Args:
        content: Full plan markdown drafted from extraction.json and templates/plan-template.md.
        ticket_id: Optional ticket id. If omitted, resolves from run-input.json.
    """
    try:
        ticket, ticket_root = _validated_ticket_root(ticket_id)
        if not str(content or "").strip():
            return {"ok": False, "error": "content must not be empty"}
        if not (ticket_root / "extraction.json").exists():
            return {"ok": False, "error": "extraction.json is missing; run download_and_extract_cql first"}

        plan_path = ticket_root / "plan.md"
        _atomic_write_text(plan_path, content if content.endswith("\n") else content + "\n")
        approval_path = TicketStateManager.write_approval_status(
            ticket, False, None, "Awaiting explicit human approval."
        )

        return {
            "ok": True,
            "ticketId": ticket,
            "planPath": str(plan_path.relative_to(ROOT_DIR)).replace("\\", "/"),
            "approvalPath": str(approval_path.relative_to(ROOT_DIR)).replace("\\", "/"),
            "message": "plan.md and approval placeholder written. Call state_write_pipeline('extraction-complete') next.",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _list_rules_engine_dir(rel_dir: str) -> tuple[bool, list[str], str]:
    """List file names in a rules-engine directory: local working copy first, GitHub main as fallback."""
    rel_dir = rel_dir.strip("/")
    local_dir = _resolve_rules_engine_root() / rel_dir
    if local_dir.is_dir():
        return True, sorted(p.name for p in local_dir.iterdir() if p.is_file()), f"local:{local_dir}"
    config = _load_json_file(ORCHESTRATION_CONFIG_PATH)
    gh = config.get("githubRepo") or {}
    ok, payload, err = _github_get_file_metadata(
        owner=str(gh.get("owner") or ""), repo=str(gh.get("repo") or ""),
        path=rel_dir, ref=str(gh.get("baseBranch") or "main"),
    )
    if not ok:
        return False, [], err
    if not isinstance(payload, list):
        return False, [], f"{rel_dir} is not a directory"
    return True, sorted(str(e.get("name")) for e in payload if e.get("type") == "file"), "github-api:main"


def _changed_generated_slugs(ticket_root: Path) -> list[str]:
    changed: list[str] = []
    for generated_path in sorted((ticket_root / "drls" / "generated").glob("*.drl")):
        slug = _generated_drl_slug(generated_path)
        baseline_path = ticket_root / "drls" / "baseline" / f"{slug}.drl"
        if not baseline_path.exists() or baseline_path.read_text(
            encoding="utf-8", errors="replace"
        ) != generated_path.read_text(encoding="utf-8", errors="replace"):
            changed.append(slug)
    return changed


@mcp.tool()
def build_current_year_tests(ticket_id: str = "") -> dict[str, Any]:
    """Build every current-year Java test artifact the ticket needs, deterministically.

    For each baseline measure (baseline/<slug>-baseline.json):
    - shared DRLs are skipped (they have no test class);
    - currentYearDrlExisted=true is skipped (the current-year test already exists in rules-engine);
    - otherwise the prior-year test Year<sourceYear><Family><Slug>Test.java is located by case-insensitive
      name in <testRoot>/<family>/year<sourceYear>/, every year token is rolled to currentYear, the class
      name and package are verified, and the result is written to pr-assets/.

    Returns ok=false with `missing` when any required prior-year test cannot be found; nothing is guessed.

    Args:
        ticket_id: Optional ticket id. If omitted, resolves from run-input.json.
    """
    try:
        ticket, ticket_root = _validated_ticket_root(ticket_id)
        meta_files = sorted((ticket_root / "baseline").glob("*-baseline.json"))
        if not meta_files:
            return {"ok": False, "error": "No baseline/<slug>-baseline.json found; run the baseline stage first."}

        config = _load_json_file(ORCHESTRATION_CONFIG_PATH)
        test_root = str(
            (config.get("rulesEngineWorkspace") or {}).get("testRoot") or "src/test/java/com/ablehealth/measures"
        ).strip("/")
        pr_assets = ticket_root / "pr-assets"
        built: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []

        for meta_path in meta_files:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            slug = str(meta.get("measureSlug") or "").strip().lower()
            family = str(meta.get("measureFamilyLower") or "").strip().lower()
            current_year = str(meta.get("currentYear") or "").strip()
            source_year = str(meta.get("sourceYear") or "").strip()
            if meta.get("isShared") or "shared" in slug:
                skipped.append({"slug": slug, "reason": "shared DRL has no test class"})
                continue
            if meta.get("currentYearDrlExisted") or source_year == current_year:
                skipped.append({"slug": slug, "reason": "current-year DRL existed, so its test already exists"})
                continue
            if not (slug and family and current_year.isdigit() and source_year.isdigit()):
                return {"ok": False, "error": f"Incomplete baseline metadata in {meta_path.name}"}
            if int(source_year) > int(current_year):
                return {"ok": False, "error": f"{slug}: sourceYear {source_year} is after currentYear {current_year}"}

            source_dir = f"{test_root}/{family}/year{source_year}"
            wanted = {
                f"year{source_year}{family}{slug}test.java",
                f"year{source_year}{slug}test.java",
            }
            ok, names, where = _list_rules_engine_dir(source_dir)
            match = next((n for n in names if n.lower() in wanted), "") if ok else ""
            if not match:
                missing.append({"slug": slug, "directory": source_dir, "expectedNames": sorted(wanted), "lookup": where})
                continue

            read = read_rules_engine_file(f"{source_dir}/{match}")
            if not read.get("ok"):
                missing.append({"slug": slug, "path": f"{source_dir}/{match}", "error": read.get("error")})
                continue
            rolled, count = _replace_year_occurrences(read["content"], source_year, current_year)
            new_name, _ = _replace_year_occurrences(match, source_year, current_year)
            class_name = new_name[: -len(".java")]
            if not re.search(rf"\bclass\s+{re.escape(class_name)}\b", rolled):
                return {"ok": False, "error": f"{slug}: rolled test does not declare class {class_name}"}
            if not re.search(rf"^\s*package\s+[\w.]*year{current_year}\s*;", rolled, re.MULTILINE):
                return {"ok": False, "error": f"{slug}: rolled test package is not year{current_year}"}
            marker = next((m for m in _PUBLISH_TRUNCATION_MARKERS if m in rolled.lower()), "")
            if marker:
                return {"ok": False, "error": f"{slug}: source test contains placeholder text '{marker}'"}

            out_path = pr_assets / new_name
            _atomic_write_text(out_path, rolled)
            built.append({
                "slug": slug,
                "sourcePath": f"{source_dir}/{match}",
                "artifact": str(out_path.relative_to(ROOT_DIR)).replace("\\", "/"),
                "yearReplacements": count,
            })

        if missing:
            return {"ok": False, "ticketId": ticket, "error": "Prior-year test not found", "missing": missing,
                    "built": built, "skipped": skipped}
        return {"ok": True, "ticketId": ticket, "built": built, "skipped": skipped,
                "message": "Test artifacts ready. Call state_write_pipeline('test-artifact-ready') next."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def write_mapping_md(content: str, ticket_id: str = "") -> dict[str, Any]:
    """Write artifacts/<ticket-id>/mapping.md after conversion.

    Refuses unless every changed generated DRL has a non-empty patch and is named in the mapping,
    and the content has no placeholder text.

    Args:
        content: Full mapping markdown (changed DRLs and why, CQL define -> DRL rule mapping, affected
            measures for shared DRLs, assumptions and open questions).
        ticket_id: Optional ticket id. If omitted, resolves from run-input.json.
    """
    try:
        ticket, ticket_root = _validated_ticket_root(ticket_id)
        if not str(content or "").strip():
            return {"ok": False, "error": "content must not be empty"}
        marker = next((m for m in _PUBLISH_TRUNCATION_MARKERS if m in content.lower()), "")
        if marker:
            return {"ok": False, "error": f"mapping contains placeholder text '{marker}'"}
        changed = _changed_generated_slugs(ticket_root)
        if not changed:
            return {"ok": False, "error": "No generated DRL differs from its baseline; conversion has not been applied."}
        errors: list[str] = []
        for slug in changed:
            patch = ticket_root / "patches" / f"{ticket}-{slug}-drl.patch"
            if not patch.exists() or "@@" not in patch.read_text(encoding="utf-8", errors="replace"):
                errors.append(f"{slug}: run generate_drl_patch first (patch missing or empty)")
            target_stem = Path(_baseline_target_file_name(
                _safe_json_load(ticket_root / "baseline" / f"{slug}-baseline.json") or {}, slug
            )).stem.lower()
            if slug not in content.lower() and target_stem not in content.lower():
                errors.append(f"{slug}: changed DRL is not named in the mapping")
        if errors:
            return {"ok": False, "ticketId": ticket, "errors": errors}
        out_path = ticket_root / "mapping.md"
        _atomic_write_text(out_path, content if content.endswith("\n") else content + "\n")
        return {"ok": True, "ticketId": ticket, "path": str(out_path.relative_to(ROOT_DIR)).replace("\\", "/"),
                "changedDrls": changed}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def open_drl_pull_request(ticket_id: str = "") -> dict[str, Any]:
    """Open (or reuse) the GitHub PR for the published branch and write pr.md. Call after publish_approved_pr_files.

    Title and body are deterministic: the body is templates/pr-template.md with only the ADO ticket URL
    filled in. An already-open PR for the branch is reused. Requires approval=true.

    Args:
        ticket_id: Optional ticket id. If omitted, resolves from run-input.json.
    """
    try:
        ticket, ticket_root = _validated_ticket_root(ticket_id)
        if not bool((TicketStateManager.read_approval_status(ticket) or {}).get("approved")):
            return {"ok": False, "error": "Approval gate failed: approval.approved is not true."}
        intake = _load_json_file(ticket_root / "intake.json")
        ticket_url = str(intake.get("ticketUrl") or "").strip()
        if not ticket_url:
            return {"ok": False, "error": "intake.json has no ticketUrl for the PR traceability link"}

        template = (BUNDLE_DIR / "templates" / "pr-template.md").read_text(encoding="utf-8")
        placeholder = "<paste ticketUrl here>"
        if placeholder not in template:
            return {"ok": False, "error": f"pr-template.md no longer contains '{placeholder}'"}
        body = template.replace(placeholder, ticket_url)

        config = _load_json_file(ORCHESTRATION_CONFIG_PATH)
        gh = config.get("githubRepo") or {}
        owner, repo = str(gh.get("owner") or ""), str(gh.get("repo") or "")
        base = str(gh.get("baseBranch") or "main")
        branch = _resolve_pr_branch_name(ticket, gh)
        if not (owner and repo and branch):
            return {"ok": False, "error": "Could not resolve owner/repo/branch from config and intake"}
        branch_ref = urllib.parse.quote(branch, safe="/-_.")
        ok, _, err = _github_git_json("GET", owner, repo, f"git/ref/heads/{branch_ref}")
        if not ok:
            return {"ok": False, "error": f"Branch '{branch}' not found; run publish_approved_pr_files first ({err})"}

        measures = [Path(p).stem.upper() for p in intake.get("cqlPaths") or []]
        family = str(intake.get("measureFamily") or "").upper()
        year = str(intake.get("measureYear") or "").strip()
        subject = " ".join(x for x in [family, "/".join(measures[:6]), "DRL update", year] if x)
        title = _format_commit_message(ticket, subject)

        head = urllib.parse.quote(f"{owner}:{branch}", safe=":/")
        ok, existing, err = _github_git_json("GET", owner, repo, f"pulls?state=open&head={head}")
        if not ok:
            return {"ok": False, "error": f"Could not list PRs for '{branch}': {err}"}
        if isinstance(existing, list) and existing:
            pr, reused = existing[0], True
        else:
            ok, pr, err = _github_git_json(
                "POST", owner, repo, "pulls", {"title": title, "head": branch, "base": base, "body": body}
            )
            if not ok:
                return {"ok": False, "error": f"Create PR failed: {err}"}
            reused = False

        pr_url = str(pr.get("html_url") or "")
        pr_md = (
            f"# Pull Request\n\n- Ticket: {ticket_url}\n- PR: {pr_url}\n- Number: {pr.get('number')}\n"
            f"- Title: {pr.get('title') or title}\n- Head: {branch}\n- Base: {base}\n"
            f"- Recorded: {datetime.now().isoformat()}\n\n## Body\n\n{pr.get('body') or body}\n"
        )
        _atomic_write_text(ticket_root / "pr.md", pr_md)
        return {"ok": True, "ticketId": ticket, "prUrl": pr_url, "number": pr.get("number"),
                "branch": branch, "reusedExisting": reused, "prMd": f"artifacts/{ticket}/pr.md",
                "message": "PR ready. Call state_write_pipeline('pr-submitted') next."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def get_pr_changes(
    project_url: str,
    repository: str,
    pull_request_id: str,
) -> dict[str, Any]:
    """Fetch changed files from a pull request's latest iteration.

    Args:
        project_url: Azure DevOps project URL (e.g., https://dev.azure.com/org/project)
        repository: Repository name or ID
        pull_request_id: Pull request ID number

    Returns:
        Dict with ok, returnCode, and either cqlPaths (list) or error message
    """
    try:
        pat = os.environ.get("ADO_PAT", "").strip()
        if not pat:
            return {
                "ok": False,
                "returnCode": 1,
                "cqlPaths": [],
                "error": "ADO_PAT environment variable is required",
            }

        token = base64.b64encode(f":{pat}".encode("ascii")).decode("ascii")
        auth_header = f"Basic {token}"

        project_url = _require_ado_url(str(project_url).rstrip("/"))
        repository = str(repository).strip()
        pr_id = str(pull_request_id).strip()

        # CRITICAL: Fetch full PR details first to get commit information
        pr_details_url = (
            f"{project_url}/_apis/git/repositories/{repository}/pullRequests/{pr_id}"
            "?api-version=7.1"
        )
        
        req = urllib.request.Request(pr_details_url)
        req.add_header("Authorization", auth_header)
        req.add_header("Content-Type", "application/json")
        
        with urllib.request.urlopen(req, timeout=120) as resp:
            pr_details = json.loads(resp.read().decode("utf-8"))
        
        # Commit roles (ADO): lastMergeSourceCommit = PR head (contains the change) -> sourceCommit;
        # lastMergeTargetCommit = target/base the PR branched from (NO change) -> mergeCommit.
        # Extraction must fetch changed CQL at sourceCommit; mergeCommit is only the diff base/fallback.
        last_merge_source_commit = pr_details.get("lastMergeSourceCommit", {})
        last_merge_target_commit = pr_details.get("lastMergeTargetCommit", {})
        
        merge_commit_id = str(last_merge_target_commit.get("commitId") or "").strip()
        source_commit_id = str(last_merge_source_commit.get("commitId") or "").strip()
        
        # Fallback to legacy fields if new ones not available
        if not source_commit_id:
            source_commit_id = str(pr_details.get("sourceCommitId") or pr_details.get("lastMergeSourceCommit", {}).get("commitId") or "").strip()
        if not merge_commit_id:
            merge_commit_id = str(pr_details.get("mergeCommit") or pr_details.get("lastMergeCommit", {}).get("commitId") or "").strip()

        # Fetch PR iterations
        iterations_url = (
            f"{project_url}/_apis/git/repositories/{repository}/pullRequests/{pr_id}/iterations"
            "?api-version=7.1"
        )
        
        req = urllib.request.Request(iterations_url)
        req.add_header("Authorization", auth_header)
        req.add_header("Content-Type", "application/json")
        
        with urllib.request.urlopen(req, timeout=120) as resp:
            iterations_payload = json.loads(resp.read().decode("utf-8"))

        iterations = iterations_payload.get("value", [])
        if not iterations:
            return {
                "ok": True,
                "returnCode": 0,
                "cqlPaths": [],
                "message": "No iterations found in PR",
            }

        latest_iteration_id = max(int(it.get("id", 0)) for it in iterations)

        # Fetch changes for latest iteration
        changes_url = (
            f"{project_url}/_apis/git/repositories/{repository}/pullRequests/{pr_id}/"
            f"iterations/{latest_iteration_id}/changes?$top=2000&api-version=7.1"
        )
        
        req = urllib.request.Request(changes_url)
        req.add_header("Authorization", auth_header)
        req.add_header("Content-Type", "application/json")
        
        with urllib.request.urlopen(req, timeout=120) as resp:
            changes_payload = json.loads(resp.read().decode("utf-8"))

        cql_paths = sorted(
            {
                str((entry.get("item") or {}).get("path") or "").strip()
                for entry in changes_payload.get("changeEntries", [])
                if str((entry.get("item") or {}).get("path") or "").lower().endswith(".cql")
            }
        )

        return {
            "ok": True,
            "returnCode": 0,
            "cqlPaths": cql_paths,
            "repository": str(repository).strip(),
            "iterationId": latest_iteration_id,
            "changeCount": len(changes_payload.get("changeEntries", [])),
            "sourceCommit": source_commit_id,
            "mergeCommit": merge_commit_id,
            "sourceBranch": str(pr_details.get("sourceRefName") or "").strip(),
            "targetBranch": str(pr_details.get("targetRefName") or "").strip(),
            "mergeStatus": str(pr_details.get("mergeStatus") or "").strip(),
        }

    except urllib.error.HTTPError as e:
        return {
            "ok": False,
            "returnCode": 1,
            "cqlPaths": [],
            "error": f"HTTP {e.code}: {e.reason}",
        }
    except Exception as e:
        return {
            "ok": False,
            "returnCode": 1,
            "cqlPaths": [],
            "error": str(e),
        }


@mcp.tool()
def get_work_item_details(
    project_url: str,
    work_item_id: str,
) -> dict[str, Any]:
    """Fetch work item details from Azure DevOps using HTTPS + ADO_PAT.
    
    This tool never prompts the user and works reliably with ADO_PAT authentication.
    Returns work item fields including title, state, description, and assigned to.

    Args:
        project_url: Azure DevOps project URL (e.g., https://dev.azure.com/org/project)
        work_item_id: Work item ID number (e.g., "1067083")

    Returns:
        Dict with ok, workItem (fields dict), or error
    """
    try:
        pat = os.environ.get("ADO_PAT", "").strip()
        if not pat:
            return {
                "ok": False,
                "error": "ADO_PAT environment variable is required",
            }

        token = base64.b64encode(f":{pat}".encode("ascii")).decode("ascii")
        auth_header = f"Basic {token}"

        project_url = _require_ado_url(str(project_url).rstrip("/"))
        work_item_id = str(work_item_id).strip()

        # Fetch work item details with all fields expanded
        work_item_url = (
            f"{project_url}/_apis/wit/workitems/{work_item_id}"
            "?$expand=all&api-version=7.1"
        )
        
        req = urllib.request.Request(work_item_url)
        req.add_header("Authorization", auth_header)
        req.add_header("Content-Type", "application/json")
        
        with urllib.request.urlopen(req, timeout=120) as resp:
            work_item = json.loads(resp.read().decode("utf-8"))
        
        fields = work_item.get("fields", {})
        
        return {
            "ok": True,
            "workItem": {
                "id": work_item.get("id"),
                "url": work_item.get("url"),
                "title": fields.get("System.Title", ""),
                "state": fields.get("System.State", ""),
                "workItemType": fields.get("System.WorkItemType", ""),
                "assignedTo": fields.get("System.AssignedTo", {}).get("displayName", "") if isinstance(fields.get("System.AssignedTo"), dict) else "",
                "createdDate": fields.get("System.CreatedDate", ""),
                "changedDate": fields.get("System.ChangedDate", ""),
                "description": fields.get("System.Description", ""),
                "tags": fields.get("System.Tags", ""),
                "areaPath": fields.get("System.AreaPath", ""),
                "iterationPath": fields.get("System.IterationPath", ""),
                "allFields": fields,  # Full fields dict for additional parsing if needed
            }
        }

    except urllib.error.HTTPError as e:
        return {
            "ok": False,
            "error": f"HTTP {e.code}: {e.reason}",
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
        }


_TARGET_STEM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def _baseline_target_file_name(meta: dict[str, Any], slug: str) -> str:
    """Current-year DRL file name for a baseline: targetFileName, else sourcePath name, else <slug>.drl."""
    if not isinstance(meta, dict):
        meta = {}
    name = str(meta.get("targetFileName") or "").strip()
    if (
        name
        and "/" not in name
        and "\\" not in name
        and ".." not in name
        and name.lower().endswith(".drl")
        and len(name) > len(".drl")
    ):
        return name
    return Path(str(meta.get("sourcePath") or "")).name or f"{slug}.drl"


@mcp.tool()
def build_prior_year_measure_template(
    ticket_id: str = "",
    measure_slug: str = "",
    current_year: str = "",
    source_year: str = "",
    source_path: str = "",
    source_content: str = "",
    source_cql_path: str = "",
) -> dict[str, Any]:
    """Build baseline artifacts by fetching the prior-year DRL from GitHub and rolling the year.

    Measure slug is derived from the CQL filename in source_cql_path (e.g. TRCA.cql -> trca).
    NEVER uses intake.measureSlug (a family slug like 'trc') for DRL lookup.

    Year auto-iteration: when source_year is omitted the function tries current year, then
    year-1, then year-2 via GitHub API and stops at the first found DRL.
    Fails with a clear error if no DRL is found for any year — no placeholders are created.

    Args:
        ticket_id: Ticket ID (resolves from run-input.json when omitted).
        source_cql_path: Full CQL path from intake.cqlPaths, e.g. /measures/2026/HEDIS/TRCA.cql.
                         The measure slug (trca) is extracted from this filename automatically.
        current_year: Override the current measurement year (default: derived from source_cql_path).
        source_year: Override the source/baseline year (default: auto-iterate current→year-1→year-2).
        source_content: Raw DRL text to use instead of fetching from GitHub.
        source_path: Repo-relative path hint when source_content is also provided.
        measure_slug: Override the measure slug (rarely needed; prefer source_cql_path). Its case is
            preserved for the rules-engine file name (e.g. sharedHospiceDuringPeriod): the GitHub lookup
            tries the exact-case name first, then lowercase. Artifact files always use the lowercase slug.
    """
    try:
        resolved_ticket = _resolve_ticket_id(ticket_id)
        raw_slug = str(measure_slug or "").strip()
        if raw_slug and not _TARGET_STEM_RE.fullmatch(raw_slug):
            return {"ok": False, "returnCode": 1, "error": f"Invalid measure slug: {raw_slug!r}"}

        context = _derive_measure_context(
            resolved_ticket,
            measure_slug,
            current_year,
            source_cql_path=source_cql_path,
        )
        # Rules-engine file stem keeps the caller's casing (mixed-case shared DRLs); artifacts stay lowercase.
        target_stem = raw_slug if raw_slug and raw_slug.lower() == context["measureSlug"] else context["measureSlug"]
        lookup_stems = list(dict.fromkeys([target_stem, context["measureSlug"]]))

        current_year_value = int(context["currentYear"])
        candidate_years = [
            str(current_year_value),
            str(current_year_value - 1),
            str(current_year_value - 2),
        ]
        expected_paths = [
            (
                f"{context['drlTargetPath']}/{context['measureFamilyLower']}/"
                f"year{year}/{stem}.drl"
            )
            for year in candidate_years
            for stem in lookup_stems
        ]

        # --- Year resolution with auto-iteration ---
        # When source_year is explicitly provided, validate and use it.
        # When it is empty (the normal agent call), auto-iterate through candidate years
        # and fetch from GitHub automatically — stop at first found.
        # NEVER create placeholder DRLs; fail cleanly if nothing is found.
        normalized_source_year = str(source_year).strip()
        normalized_source_path = ""
        normalized_source_content = ""
        source_content_origin = "missing"
        github_error = ""

        years_to_try: list[str] = (
            [normalized_source_year]
            if normalized_source_year in candidate_years
            else (
                # Invalid explicit value — reject immediately.
                None
                if normalized_source_year
                else candidate_years
            )
        )

        if years_to_try is None:
            return {
                "ok": False,
                "returnCode": 1,
                "ticketId": resolved_ticket,
                "measureSlug": context["measureSlug"],
                "error": "source_year must be one of the candidate years (current, year-1, or year-2)",
                "expectedSourceYears": candidate_years,
                "providedSourceYear": normalized_source_year,
            }

        github_errors: list[str] = []
        for try_year in years_to_try:
            # If caller passed explicit content, try to coerce it for this year.
            if source_content.strip() or source_path.strip():
                candidate_path = _normalize_baseline_source_path(
                    source_path,
                    expected_paths=expected_paths,
                    normalized_source_year=try_year,
                    drl_target_path=context["drlTargetPath"],
                    measure_family_lower=context["measureFamilyLower"],
                    measure_slug=target_stem,
                )
                coerced_content, coerced_origin = _coerce_source_content(source_content, source_path)
                if coerced_content.strip():
                    normalized_source_year = try_year
                    normalized_source_path = candidate_path
                    normalized_source_content = coerced_content
                    source_content_origin = coerced_origin
                    break

            # Auto-fetch from GitHub for this year: exact-case file name first, then lowercase.
            for try_stem in lookup_stems:
                try_path = (
                    f"{context['drlTargetPath']}/{context['measureFamilyLower']}/"
                    f"year{try_year}/{try_stem}.drl"
                )
                ok, github_text, github_error = _github_fetch_file_text(
                    owner=context["owner"],
                    repo=context["repo"],
                    path=try_path,
                    ref=context["baseBranch"],
                )
                if ok and github_text.strip():
                    normalized_source_year = try_year
                    normalized_source_path = try_path
                    normalized_source_content = github_text
                    source_content_origin = (
                        f"github-api-auto:{context['baseBranch']}:{try_path}"
                    )
                    break
                github_errors.append(f"{try_path}: {github_error}")
            if normalized_source_content.strip():
                break

        if not normalized_source_content.strip():
            return {
                "ok": False,
                "returnCode": 1,
                "ticketId": resolved_ticket,
                "measureSlug": context["measureSlug"],
                "error": (
                    "Baseline DRL not found in GitHub for any lookback year. "
                    "No placeholder will be created — provide the DRL or verify the measure exists."
                ),
                "pathsAttempted": [e.split(":")[0] for e in github_errors],
                "githubErrors": github_errors,
                "lookbackYears": candidate_years,
                "hint": (
                    "Confirm the measure slug is correct (it should match the DRL filename, "
                    "e.g. 'trca' from TRCA.cql, not the family slug 'trc'). "
                    "Check that GITHUB_TOKEN is set and has read access to the repo."
                ),
            }

        rolled_content, replacement_count = _replace_year_occurrences(
            normalized_source_content,
            normalized_source_year,
            context["currentYear"],
        )

        # When the matched baseline DRL is the CURRENT year, the current-year DRL already exists.
        # In that case the current-year test (Year<YYYY><Family><Num>Test.java) is guaranteed to
        # exist too, so the Test Builder can clone it directly with NO year roll-forward.
        current_year_drl_existed = normalized_source_year == str(current_year_value)
        # Metadata JSON goes to ticket-first baseline dir.
        baseline_dir = ROOT_DIR / "artifacts" / resolved_ticket / "baseline"
        baseline_dir.mkdir(parents=True, exist_ok=True)

        # DRL goes to canonical ticket-first location.
        ticket_root = ROOT_DIR / "artifacts" / resolved_ticket
        drls_baseline_dir = ticket_root / "drls" / "baseline"
        drls_baseline_dir.mkdir(parents=True, exist_ok=True)
        baseline_drl_path = drls_baseline_dir / f"{context['measureSlug']}.drl"
        baseline_drl_path.write_text(rolled_content, encoding="utf-8")

        metadata = {
            "ticketId": resolved_ticket,
            "measureSlug": context["measureSlug"],
            "measureFamilyLower": context["measureFamilyLower"],
            "currentYear": context["currentYear"],
            "sourceYear": normalized_source_year,
            "currentYearDrlExisted": current_year_drl_existed,
            "sourceRepo": f"{context['owner']}/{context['repo']}",
            "sourceBranch": context["baseBranch"],
            "sourcePath": normalized_source_path,
            "targetFileName": Path(normalized_source_path).name or f"{target_stem}.drl",
            "sourceCqlPath": context["sourceCqlPath"],
            "sourceContentOrigin": source_content_origin,
            "yearReplacementCount": replacement_count,
            "generatedAt": datetime.now().isoformat(),
            "lookupWindowYears": candidate_years,
        }

        metadata_path = baseline_dir / f"{context['measureSlug']}-baseline.json"

        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        # CQL diffs are produced earlier by the extraction stage (download_and_extract_cql ->
        # patches/<slug>-cql-diff.patch). The baseline stage only fetches the prior-year DRL
        # and rolls the year forward; it does not generate CQL diffs.
        return {
            "ok": True,
            "returnCode": 0,
            "ticketId": resolved_ticket,
            "measureSlug": context["measureSlug"],
            "currentYear": context["currentYear"],
            "sourceYear": normalized_source_year,
            "currentYearDrlExisted": current_year_drl_existed,
            "sourcePath": normalized_source_path,
            "targetFileName": metadata["targetFileName"],
            "templatePath": str(baseline_drl_path.relative_to(ROOT_DIR)).replace("\\", "/"),
            "metadataPath": str(metadata_path.relative_to(ROOT_DIR)).replace("\\", "/"),
            "yearReplacementCount": replacement_count,
            "lookbackYears": candidate_years,
        }
    except Exception as e:
        return {
            "ok": False,
            "returnCode": 1,
            "error": str(e),
        }


@mcp.tool()
def build_shared_drl_baseline(ticket_id: str = "") -> dict[str, Any]:
    """Detect and fetch shared DRL baseline when multiple measures in a family exist.
    
    This tool automatically:
    1. Reads intake.cqlPaths and detects if multiple measures belong to a family
    2. Checks if a shared DRL exists in GitHub (e.g., trcshared.drl for TRC family)
    3. Fetches and saves the shared DRL baseline if found
    
    Examples of shared DRL patterns:
    - TRC family: trca, trcb, trce, trce7, trcm → trcshared.drl
    - AAP family: aap1, aap2 → aapshared.drl  
    - FUM family: fum7, fum30 → fumshared.drl
    - UOP family: uopa, uopb, uopc → uopshared.drl
    
    Args:
        ticket_id: Optional ticket ID (resolves from run-input.json if omitted)
    
    Returns:
        Dict with:
        - ok: True if shared DRL was found and fetched
        - shared_found: True if a shared DRL pattern was detected
        - family_prefix: The measure family prefix (e.g., "trc")
        - templatePath: Path to the saved shared DRL file
    """
    try:
        resolved_ticket = _resolve_ticket_id(ticket_id)
        
        # Load intake to get CQL paths
        intake_path = ROOT_DIR / "artifacts" / resolved_ticket / "intake.json"
        if not intake_path.exists():
            return {
                "ok": False,
                "returnCode": 1,
                "error": f"Missing intake artifact: {intake_path}",
            }
        
        intake = _load_json_file(intake_path)
        cql_paths = [str(p).strip() for p in (intake.get("cqlPaths") or []) if str(p).strip()]
        # Extract family directory from the first CQL path (e.g., "HEDIS" from "/measures/2026/HEDIS/TRCA.cql")
        # This must match _derive_measure_context which also uses the CQL path segment, NOT intake.measureFamily.
        # intake.measureFamily is a measure-domain identifier (e.g., "TRC") not the directory segment ("hedis").
        first_cql = cql_paths[0] if cql_paths else ""
        family_dir_match = re.search(r"/20\d{2}/([^/]+)/", first_cql)
        measure_family_lower = family_dir_match.group(1).strip().lower() if family_dir_match else "hedis"
        measure_year = str(intake.get("measureYear") or "2026").strip()
        
        if not cql_paths:
            return {
                "ok": False,
                "shared_found": False,
                "error": "No CQL paths found in intake",
            }
        
        # Detect shared pattern
        family_prefix, has_shared_in_intake, shared_cql_path = _detect_shared_cql_pattern(
            cql_paths,
            measure_family_lower,
        )
        
        if not family_prefix or len(cql_paths) < 2:
            return {
                "ok": True,
                "shared_found": False,
                "message": "Single measure or no clear family pattern detected - no shared DRL needed",
                "family_prefix": family_prefix,
            }
        
        # Load config to get GitHub repo details
        config = _load_json_file(ORCHESTRATION_CONFIG_PATH)
        github_repo = config.get("githubRepo") or {}
        owner = str(github_repo.get("owner") or "").strip()
        repo = str(github_repo.get("repo") or "").strip()
        base_branch = str(github_repo.get("baseBranch") or "main").strip() or "main"
        drl_target_path = str(github_repo.get("drlTargetPath") or "").strip().strip("/")
        
        if not owner or not repo or not drl_target_path:
            return {
                "ok": False,
                "returnCode": 1,
                "error": "githubRepo owner/repo/drlTargetPath are required in config",
            }
        
        # Try different shared DRL naming patterns (lowercase and camelCase)
        shared_slug_candidates = [
            f"{family_prefix.lower()}shared",  # trcshared
            f"{family_prefix}Shared",          # TRCShared (rare)
            f"{family_prefix}shared",          # TRCshared
        ]
        
        # Try current year first, then fallback years
        current_year_value = int(measure_year)
        candidate_years = [
            str(current_year_value),
            str(current_year_value - 1),
            str(current_year_value - 2),
        ]
        
        found_year = ""
        found_slug = ""
        found_path = ""
        found_content = ""
        github_errors: list[str] = []
        
        for try_year in candidate_years:
            for try_slug in shared_slug_candidates:
                try_path = (
                    f"{drl_target_path}/{measure_family_lower}/"
                    f"year{try_year}/{try_slug}.drl"
                )
                
                ok, github_text, github_error = _github_fetch_file_text(
                    owner=owner,
                    repo=repo,
                    path=try_path,
                    ref=base_branch,
                )
                
                if ok and github_text.strip():
                    found_year = try_year
                    found_slug = try_slug
                    found_path = try_path
                    found_content = github_text
                    break
                
                if github_error != "not-found":
                    github_errors.append(f"{try_path}: {github_error}")
            
            if found_content:
                break
        
        if not found_content:
            return {
                "ok": True,
                "shared_found": False,
                "message": f"No shared DRL found for family '{family_prefix}' in GitHub",
                "family_prefix": family_prefix,
                "paths_attempted": [e.split(":")[0] for e in github_errors],
                "note": "This is normal for families that don't use shared DRL patterns",
            }
        
        # Roll year forward if needed
        rolled_content, replacement_count = _replace_year_occurrences(
            found_content,
            found_year,
            measure_year,
        )
        
        # Save to baseline directory
        baseline_dir = ROOT_DIR / "artifacts" / resolved_ticket / "baseline"
        baseline_dir.mkdir(parents=True, exist_ok=True)
        
        drls_baseline_dir = ROOT_DIR / "artifacts" / resolved_ticket / "drls" / "baseline"
        drls_baseline_dir.mkdir(parents=True, exist_ok=True)
        baseline_drl_path = drls_baseline_dir / f"{found_slug}.drl"
        baseline_drl_path.write_text(rolled_content, encoding="utf-8")
        
        # Save metadata
        metadata = {
            "ticketId": resolved_ticket,
            "measureSlug": found_slug,
            "measureFamilyLower": measure_family_lower,
            "familyPrefix": family_prefix,
            "isShared": True,
            "currentYear": measure_year,
            "sourceYear": found_year,
            "sourceRepo": f"{owner}/{repo}",
            "sourceBranch": base_branch,
            "sourcePath": found_path,
            "targetFileName": f"{found_slug}.drl",
            "yearReplacementCount": replacement_count,
            "generatedAt": datetime.now().isoformat(),
            "relatedMeasures": [Path(p).stem.lower() for p in cql_paths],
        }
        
        metadata_path = baseline_dir / f"{found_slug}-baseline.json"
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        
        return {
            "ok": True,
            "shared_found": True,
            "returnCode": 0,
            "ticketId": resolved_ticket,
            "family_prefix": family_prefix,
            "shared_slug": found_slug,
            "sourceYear": found_year,
            "sourcePath": found_path,
            "templatePath": str(baseline_drl_path.relative_to(ROOT_DIR)).replace("\\", "/"),
            "metadataPath": str(metadata_path.relative_to(ROOT_DIR)).replace("\\", "/"),
            "yearReplacementCount": replacement_count,
            "relatedMeasures": metadata["relatedMeasures"],
        }
        
    except Exception as e:
        return {
            "ok": False,
            "returnCode": 1,
            "error": str(e),
        }


@mcp.tool()
def state_set_current_ticket(ticket: str) -> dict[str, Any]:
    """Set the current ticket (run-input.json) from a ticket ID or work-item URL."""
    try:
        raw = str(ticket or "").strip().rstrip("/")
        candidate = raw.split("/")[-1].split("?")[0]
        if not candidate.isdigit() or len(candidate) > 20:
            return {"ok": False, "error": f"Cannot extract a numeric ticket ID from: {raw!r}"}
        run_input = TicketStateManager.RUN_INPUT_FILE
        run_input.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, str] = {"ticketId": candidate}
        if raw.lower().startswith("http"):
            payload["ticketUrl"] = raw
        run_input.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return {"ok": True, "ticketId": candidate}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def read_artifact(relative_path: str, ticket_id: str = "") -> dict[str, Any]:
    """Read a text file under artifacts/<ticket-id>/ (for example intake.json or extraction.json)."""
    try:
        resolved_ticket = _resolve_ticket_id(ticket_id)
        base = (ROOT_DIR / "artifacts" / resolved_ticket).resolve()
        target = (base / str(relative_path or "").strip().lstrip("/\\")).resolve()
        if base != target and base not in target.parents:
            return {"ok": False, "error": "Path escapes the ticket artifacts folder."}
        if not target.is_file():
            return {"ok": False, "error": f"Not found: {relative_path}"}
        return {
            "ok": True,
            "ticketId": resolved_ticket,
            "path": str(target.relative_to(base)),
            "content": target.read_text(encoding="utf-8-sig"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def export_artifacts(ticket_id: str = "", destination: str = "") -> dict[str, Any]:
    """Copy a ticket's artifacts out of the plugin data folder so the user can review them.

    destination defaults to drl-output/<ticket-id> under the server's working directory.
    """
    try:
        resolved_ticket = _resolve_ticket_id(ticket_id)
        source = (ROOT_DIR / "artifacts" / resolved_ticket).resolve()
        if not source.is_dir():
            return {"ok": False, "error": f"No artifacts for ticket {resolved_ticket}."}
        raw_dest = str(destination or "").strip()
        dest = (Path(raw_dest) if raw_dest else Path("drl-output") / resolved_ticket)
        dest = (dest if dest.is_absolute() else Path.cwd() / dest).resolve()
        if dest == ROOT_DIR or ROOT_DIR in dest.parents or source in dest.parents:
            return {"ok": False, "error": "Destination must be outside the plugin data folder."}
        if dest.exists() and not dest.is_dir():
            return {"ok": False, "error": f"Destination is a file: {dest}"}
        shutil.copytree(source, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("*.tmp"))
        files = sorted(
            str(p.relative_to(dest)).replace("\\", "/") for p in dest.rglob("*") if p.is_file()
        )
        return {
            "ok": True,
            "ticketId": resolved_ticket,
            "sourceDir": str(source),
            "exportedTo": str(dest),
            "fileCount": len(files),
            "files": files,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def state_get_current_ticket() -> dict[str, Any]:
    """Resolve current ticket ID from run-input.json."""
    try:
        return {
            "ok": True,
            "ticketId": TicketStateManager.get_current_ticket_id(),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
        }


@mcp.tool()
def state_read_pipeline(ticket_id: str = "") -> dict[str, Any]:
    """Read pipeline status for a ticket (or current ticket when omitted)."""
    try:
        resolved_ticket = _resolve_ticket_id(ticket_id)
        return {
            "ok": True,
            "ticketId": resolved_ticket,
            "pipeline": TicketStateManager.read_pipeline_status(resolved_ticket),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
        }


@mcp.tool()
def state_read_approval(ticket_id: str = "") -> dict[str, Any]:
    """Read approval status for a ticket (or current ticket when omitted)."""
    try:
        resolved_ticket = _resolve_ticket_id(ticket_id)
        return {
            "ok": True,
            "ticketId": resolved_ticket,
            "approval": TicketStateManager.read_approval_status(resolved_ticket),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
        }


@mcp.tool()
def state_write_pipeline(stage: str, details: str = "", ticket_id: str = "") -> dict[str, Any]:
    """Write pipeline status using canonical stage validation."""
    try:
        resolved_ticket = _resolve_ticket_id(ticket_id)
        path = TicketStateManager.write_pipeline_status(resolved_ticket, stage, details)
        return {
            "ok": True,
            "ticketId": resolved_ticket,
            "stage": stage,
            "path": str(path),
            "artifactBundle": {
                "path": f"artifacts/{resolved_ticket}",
                "counts": {"patches": 0, "cqls": 0, "drls": 0},
                "sync": "skipped-non-destructive",
            },
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
        }


@mcp.tool()
def state_write_approval(
    approved: bool,
    approved_by: str = "",
    notes: str = "",
    ticket_id: str = "",
) -> dict[str, Any]:
    """Write approval status for a ticket (or current ticket when omitted)."""
    try:
        resolved_ticket = _resolve_ticket_id(ticket_id)
        normalized_approver = approved_by.strip() or None
        if approved and not normalized_approver:
            return {"ok": False, "error": "approved_by is required when approved=true"}
        path = TicketStateManager.write_approval_status(
            resolved_ticket,
            bool(approved),
            normalized_approver,
            notes,
        )
        return {
            "ok": True,
            "ticketId": resolved_ticket,
            "approved": bool(approved),
            "path": str(path),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
        }


@mcp.tool()
def state_reset_ticket_baseline(
    ticket_id: str = "",
    confirm: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    """Reset a ticket to first-time baseline.

    This removes ticket-specific artifacts and per-ticket state so the ticket
    appears as if it has never run in the local workflow.

    DESTRUCTIVE: deletes the entire artifacts/<ticket-id>/ folder. To prevent
    accidental invocation (e.g. an agent calling reset as part of a normal flow),
    this fails closed: it deletes nothing unless `confirm=True` is passed explicitly.
    Only the dedicated `drlTicketReset` agent should pass confirm=True.

    Args:
        ticket_id: Optional ticket id. If omitted, resolves from run-input.json.
        confirm: Must be True to actually delete. Defaults to False (no-op refusal).
        reason: Why the user asked for the reset; stored in state/reset-logs/<ticket>-reset-log.json.
    """
    try:
        resolved_ticket = _resolve_ticket_id(ticket_id)

        if not confirm:
            return {
                "ok": False,
                "ticketId": resolved_ticket,
                "refused": True,
                "deleted": [],
                "skipped": [],
                "error": (
                    "Reset refused: confirm=True is required to delete ticket artifacts. "
                    "This guard prevents accidental resets during intake/conversion. "
                    "If you genuinely intend to reset, run the /drlTicketReset agent."
                ),
            }

        deleted: list[str] = []
        skipped: list[str] = []

        # Deterministic artifact cleanup for this ticket.
        targets = [
            ROOT_DIR / "artifacts" / resolved_ticket,
        ]

        for path in sorted(set(targets)):
            if _safe_remove_path(path):
                deleted.append(str(path.relative_to(ROOT_DIR)).replace("\\", "/"))
            else:
                skipped.append(str(path.relative_to(ROOT_DIR)).replace("\\", "/"))

        # Normalize and update the canonical master index (shared across all tickets).
        index_path = ROOT_DIR / "state" / "tickets-index.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        if index_path.exists() and index_path.is_dir():
            return {
                "ok": False,
                "ticketId": resolved_ticket,
                "error": "tickets-index.json is a directory; replace with a JSON file",
                "deleted": deleted,
                "skipped": skipped,
            }

        if index_path.exists():
            index = json.loads(index_path.read_text(encoding="utf-8-sig"))
        else:
            index = {
                "currentTicketId": None,
                "lastUpdated": "",
                "tickets": {},
            }

        tickets_map = index.get("tickets") or {}
        if isinstance(tickets_map, dict):
            tickets_map.pop(resolved_ticket, None)
        else:
            tickets_map = {}
        index["tickets"] = tickets_map

        if index.get("currentTicketId") == resolved_ticket:
            index["currentTicketId"] = None

        index["lastUpdated"] = datetime.now().isoformat()
        index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")

        log_path = ROOT_DIR / "state" / "reset-logs" / f"{resolved_ticket}-reset-log.json"
        _atomic_write_text(log_path, json.dumps({
            "ticketId": resolved_ticket,
            "deleted": sorted(set(deleted)),
            "skipped": sorted(set(skipped)),
            "timestamp": datetime.now().isoformat(),
            "reason": reason.strip() or "User-initiated reset via drlTicketReset agent",
        }, indent=2) + "\n")

        return {
            "ok": True,
            "ticketId": resolved_ticket,
            "deleted": sorted(set(deleted)),
            "skipped": sorted(set(skipped)),
            "indexPath": "state/tickets-index.json",
            "resetLog": str(log_path.relative_to(ROOT_DIR)).replace("\\", "/"),
            "baselineState": "no ticket-scoped state",
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
        }


# ============================================================================
# Learning Engine Tools (Passive, Non-Blocking)
# ============================================================================


@mcp.tool()
def learning_record_execution(
    ticket_id: str,
    stage: str,
    elapsed_seconds: float,
    success: bool,
    details: str = "",
) -> dict[str, Any]:
    """Record execution metrics for a pipeline stage (non-blocking).

    Args:
        ticket_id: Ticket being processed
        stage: Pipeline stage (intake, extraction, approval, conversion, pr)
        elapsed_seconds: Time taken for stage
        success: Whether stage succeeded
        details: Optional context info

    Returns:
        Dict with ok status (always returns ok=true for non-blocking)
    """
    try:
        LearningEngine.record_execution(ticket_id, stage, elapsed_seconds, success, details)
        return {"ok": True, "recorded": True}
    except Exception as e:
        # Silent fail: never propagate errors
        return {"ok": True, "recorded": False, "reason": str(e)}


@mcp.tool()
def learning_record_cql_patterns(ticket_id: str, cql_content: str) -> dict[str, Any]:
    """Analyze CQL and record patterns (non-blocking).

    Args:
        ticket_id: Ticket being processed
        cql_content: CQL text to analyze

    Returns:
        Dict with ok status (always returns ok=true for non-blocking)
    """
    try:
        LearningEngine.record_cql_patterns(ticket_id, cql_content)
        return {"ok": True, "recorded": True}
    except Exception as e:
        # Silent fail: never propagate errors
        return {"ok": True, "recorded": False, "reason": str(e)}


@mcp.tool()
def learning_record_error(
    ticket_id: str,
    stage: str,
    error_type: str,
    error_message: str,
) -> dict[str, Any]:
    """Record error pattern for troubleshooting (non-blocking).

    Args:
        ticket_id: Ticket where error occurred
        stage: Pipeline stage where error occurred
        error_type: Error category (validation, mcp-call, parsing, etc.)
        error_message: Error description

    Returns:
        Dict with ok status (always returns ok=true for non-blocking)
    """
    try:
        LearningEngine.record_error(ticket_id, stage, error_type, error_message)
        return {"ok": True, "recorded": True}
    except Exception as e:
        # Silent fail: never propagate errors
        return {"ok": True, "recorded": False, "reason": str(e)}


@mcp.tool()
def learning_get_summary() -> dict[str, Any]:
    """Get current learning summary (aggregated metrics/patterns/errors).

    Returns:
        Dict with metrics, patterns, errors summary
    """
    try:
        return {"ok": True, "summary": LearningEngine.get_summary()}
    except Exception as e:
        # Silent fail: return empty summary
        return {"ok": False, "error": str(e)}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # FastAPI-mounted subapps do not automatically drive the child lifespan.
    # Run the MCP session manager explicitly so tool requests can be handled.
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Local Python MCP Server", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    # Preflight requires status=ok, so a missing config fails fast instead of inside every tool.
    if not ORCHESTRATION_CONFIG_PATH.is_file():
        return {"status": "error", "error": f"orchestration config not found at {ORCHESTRATION_CONFIG_PATH}"}
    return {"status": "ok"}


if hasattr(mcp, "streamable_http_app"):
    # Be tolerant to SDK/client path expectations:
    # - Some variants expose MCP transport at subapp root.
    # - Others expose it under /mcp inside the subapp.
    # Mount both prefixes so clients configured for /mcp do not get 404.
    app.mount("/mcp", _mcp_asgi_app)
    app.mount("/", _mcp_asgi_app)
elif hasattr(mcp, "sse_app"):
    # Compatibility fallback for older SDK variants.
    # sse_app exposes its own route set (for example /sse, /messages).
    app.mount("/", mcp.sse_app())
else:
    raise RuntimeError("Installed MCP Python SDK does not expose an HTTP app transport.")


def main() -> int:
    if "--stdio" in sys.argv:
        # Run as a direct stdio MCP server (no separate HTTP process required).
        try:
            mcp.run(transport="stdio")
        except TypeError:
            # Older SDK variants may not accept explicit transport argument.
            mcp.run()
        return 0

    import uvicorn

    uvicorn.run(app, host=APP_HOST, port=APP_PORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
