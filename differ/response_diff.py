"""
Compares actual API responses between versions.
Performs deep structural and value comparison with severity classification.
"""

from typing import Dict, Any, List, Optional
from deepdiff import DeepDiff
import json


SEVERITY_BREAKING = "BREAKING"
SEVERITY_ADDITIVE = "ADDITIVE"
SEVERITY_MODIFIED = "MODIFIED"
SEVERITY_COSMETIC = "COSMETIC"


def diff_responses(response_a: Dict[str, Any], response_b: Dict[str, Any],
                   version_a: str = "A", version_b: str = "B") -> Dict[str, Any]:
    """
    Compare two live API responses in detail.
    Returns structured comparison with severity ratings.
    """
    result = {
        "version_a": version_a,
        "version_b": version_b,
        "status_code_diff": None,
        "header_diffs": [],
        "body_diffs": [],
        "structure_diffs": [],
        "timing_diff": None,
        "overall_severity": SEVERITY_COSMETIC,
        "errors": [],
    }

    # Check for errors
    if response_a.get("error"):
        result["errors"].append(f"{version_a}: {response_a['error']}")
    if response_b.get("error"):
        result["errors"].append(f"{version_b}: {response_b['error']}")
    if result["errors"]:
        result["overall_severity"] = SEVERITY_BREAKING
        return result

    # Status code comparison
    sc_a = response_a.get("status_code")
    sc_b = response_b.get("status_code")
    if sc_a != sc_b:
        result["status_code_diff"] = {
            version_a: sc_a,
            version_b: sc_b,
            "severity": SEVERITY_BREAKING,
            "detail": f"Status code changed from {sc_a} to {sc_b}",
        }

    # Response time comparison
    time_a = response_a.get("response_time_ms", 0)
    time_b = response_b.get("response_time_ms", 0)
    if time_a > 0 and time_b > 0:
        pct_change = ((time_b - time_a) / time_a * 100) if time_a > 0 else 0
        result["timing_diff"] = {
            version_a: time_a,
            version_b: time_b,
            "difference_ms": round(time_b - time_a, 2),
            "percentage_change": round(pct_change, 1),
        }

    # Body comparison
    body_a = response_a.get("body")
    body_b = response_b.get("body")

    if body_a is not None and body_b is not None:
        if isinstance(body_a, (dict, list)) and isinstance(body_b, (dict, list)):
            result["body_diffs"] = _deep_diff_bodies(body_a, body_b, version_a, version_b)
            result["structure_diffs"] = _structure_diff(body_a, body_b, version_a, version_b)
        elif body_a != body_b:
            result["body_diffs"].append({
                "path": "(root)",
                "change": "value_changed",
                "severity": SEVERITY_MODIFIED,
                version_a: str(body_a)[:200],
                version_b: str(body_b)[:200],
            })

    # Header comparison (selected important headers)
    important_headers = [
        "content-type", "x-ratelimit-limit", "x-ratelimit-remaining",
        "x-total-count", "x-pagination-total",
    ]
    headers_a = {k.lower(): v for k, v in response_a.get("headers", {}).items()}
    headers_b = {k.lower(): v for k, v in response_b.get("headers", {}).items()}
    for hdr in important_headers:
        val_a = headers_a.get(hdr)
        val_b = headers_b.get(hdr)
        if val_a != val_b and (val_a is not None or val_b is not None):
            result["header_diffs"].append({
                "header": hdr,
                version_a: val_a,
                version_b: val_b,
            })

    # Determine overall severity
    all_severities = []
    if result["status_code_diff"]:
        all_severities.append(SEVERITY_BREAKING)
    for d in result["body_diffs"]:
        all_severities.append(d.get("severity", SEVERITY_COSMETIC))
    for d in result["structure_diffs"]:
        all_severities.append(d.get("severity", SEVERITY_COSMETIC))

    severity_order = [SEVERITY_BREAKING, SEVERITY_MODIFIED, SEVERITY_ADDITIVE, SEVERITY_COSMETIC]
    for sev in severity_order:
        if sev in all_severities:
            result["overall_severity"] = sev
            break

    return result


def _deep_diff_bodies(body_a: Any, body_b: Any,
                       version_a: str, version_b: str) -> List[Dict[str, Any]]:
    """Use DeepDiff for detailed body comparison."""
    changes = []
    try:
        dd = DeepDiff(body_a, body_b, ignore_order=True, verbose_level=2)

        # Values changed
        for path, change in dd.get("values_changed", {}).items():
            changes.append({
                "path": _clean_path(path),
                "change": "value_changed",
                "severity": SEVERITY_MODIFIED,
                version_a: _safe_str(change.get("old_value")),
                version_b: _safe_str(change.get("new_value")),
            })

        # Type changes
        for path, change in dd.get("type_changes", {}).items():
            changes.append({
                "path": _clean_path(path),
                "change": "type_changed",
                "severity": SEVERITY_BREAKING,
                version_a: f"{type(change.get('old_value')).__name__}: {_safe_str(change.get('old_value'))}",
                version_b: f"{type(change.get('new_value')).__name__}: {_safe_str(change.get('new_value'))}",
            })

        # Keys added
        for path in dd.get("dictionary_item_added", []):
            changes.append({
                "path": _clean_path(str(path)),
                "change": "field_added",
                "severity": SEVERITY_ADDITIVE,
                version_a: "(absent)",
                version_b: "(present)",
            })

        # Keys removed
        for path in dd.get("dictionary_item_removed", []):
            changes.append({
                "path": _clean_path(str(path)),
                "change": "field_removed",
                "severity": SEVERITY_BREAKING,
                version_a: "(present)",
                version_b: "(absent)",
            })

        # Items added to iterables
        for path, item in dd.get("iterable_item_added", {}).items():
            changes.append({
                "path": _clean_path(str(path)),
                "change": "item_added",
                "severity": SEVERITY_ADDITIVE,
                version_a: "(absent)",
                version_b: _safe_str(item),
            })

        # Items removed from iterables
        for path, item in dd.get("iterable_item_removed", {}).items():
            changes.append({
                "path": _clean_path(str(path)),
                "change": "item_removed",
                "severity": SEVERITY_BREAKING,
                version_a: _safe_str(item),
                version_b: "(absent)",
            })

    except Exception as e:
        changes.append({
            "path": "(error)",
            "change": "diff_error",
            "severity": SEVERITY_MODIFIED,
            "detail": str(e),
        })

    return changes


def _structure_diff(body_a: Any, body_b: Any,
                     version_a: str, version_b: str) -> List[Dict[str, Any]]:
    """Compare the structural shape (keys at each level) of two responses."""
    diffs = []

    if isinstance(body_a, dict) and isinstance(body_b, dict):
        keys_a = set(body_a.keys())
        keys_b = set(body_b.keys())

        for k in sorted(keys_b - keys_a):
            diffs.append({
                "path": k,
                "change": "key_added",
                "severity": SEVERITY_ADDITIVE,
                "detail": f"New key '{k}' in {version_b}",
            })
        for k in sorted(keys_a - keys_b):
            diffs.append({
                "path": k,
                "change": "key_removed",
                "severity": SEVERITY_BREAKING,
                "detail": f"Key '{k}' removed in {version_b}",
            })

        # Recurse into common keys
        for k in keys_a & keys_b:
            sub_diffs = _structure_diff(body_a[k], body_b[k], version_a, version_b)
            for sd in sub_diffs:
                sd["path"] = f"{k}.{sd['path']}"
                diffs.append(sd)

    elif isinstance(body_a, list) and isinstance(body_b, list):
        if len(body_a) > 0 and len(body_b) > 0:
            if isinstance(body_a[0], dict) and isinstance(body_b[0], dict):
                sub_diffs = _structure_diff(body_a[0], body_b[0], version_a, version_b)
                for sd in sub_diffs:
                    sd["path"] = f"[].{sd['path']}"
                    diffs.append(sd)

    return diffs


def _clean_path(path: str) -> str:
    """Clean DeepDiff path notation to readable format."""
    return path.replace("root", "$").replace("['", ".").replace("']", "").replace("[", "[").replace("]", "]")


def _safe_str(val: Any, max_len: int = 200) -> str:
    """Safely convert a value to string, truncating if needed."""
    if val is None:
        return "null"
    s = str(val)
    if len(s) > max_len:
        return s[:max_len] + "..."
    return s
