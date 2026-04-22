"""
Deep comparison of request/response schemas between API versions.
Uses deepdiff for structural comparison and adds severity classification.
"""

from typing import Dict, Any, List, Optional
from deepdiff import DeepDiff
from parser.normalizer import (
    extract_endpoints, get_request_schema, get_response_schema,
    flatten_schema, get_parameters_dict
)


SEVERITY_BREAKING = "BREAKING"
SEVERITY_ADDITIVE = "ADDITIVE"
SEVERITY_MODIFIED = "MODIFIED"
SEVERITY_COSMETIC = "COSMETIC"


def classify_change(change_type: str, path: str, old_val: Any = None, new_val: Any = None) -> str:
    """Classify a schema change by severity."""
    if change_type == "removed":
        return SEVERITY_BREAKING
    if change_type == "type_changed":
        return SEVERITY_BREAKING
    if change_type == "added":
        return SEVERITY_ADDITIVE
    if change_type == "required_added":
        return SEVERITY_BREAKING
    if change_type == "required_removed":
        return SEVERITY_ADDITIVE
    if change_type == "enum_changed":
        # If enum values were removed, it's breaking
        if isinstance(old_val, list) and isinstance(new_val, list):
            if set(old_val) - set(new_val):
                return SEVERITY_BREAKING
        return SEVERITY_MODIFIED
    return SEVERITY_MODIFIED


def diff_flat_schemas(schema_a: Optional[Dict], schema_b: Optional[Dict]) -> List[Dict[str, Any]]:
    """
    Compare two schemas by flattening them and diffing field-by-field.
    Returns a list of change records with severity.
    """
    changes = []

    if schema_a is None and schema_b is None:
        return changes
    if schema_a is None:
        schema_a = {}
    if schema_b is None:
        schema_b = {}

    fields_a = flatten_schema(schema_a) if schema_a else {}
    fields_b = flatten_schema(schema_b) if schema_b else {}

    all_fields = set(fields_a.keys()) | set(fields_b.keys())

    for field in sorted(all_fields):
        in_a = field in fields_a
        in_b = field in fields_b

        if in_a and not in_b:
            changes.append({
                "field": field,
                "change": "removed",
                "severity": SEVERITY_BREAKING,
                "old": fields_a[field],
                "new": None,
                "detail": f"Field '{field}' was removed",
            })
        elif not in_a and in_b:
            is_required = fields_b[field].get("required", False)
            sev = SEVERITY_BREAKING if is_required else SEVERITY_ADDITIVE
            changes.append({
                "field": field,
                "change": "added",
                "severity": sev,
                "old": None,
                "new": fields_b[field],
                "detail": f"Field '{field}' was added" + (" (required)" if is_required else " (optional)"),
            })
        else:
            fa = fields_a[field]
            fb = fields_b[field]

            # Type change
            if fa.get("type") != fb.get("type"):
                changes.append({
                    "field": field,
                    "change": "type_changed",
                    "severity": SEVERITY_BREAKING,
                    "old": fa,
                    "new": fb,
                    "detail": f"Type changed from '{fa.get('type')}' to '{fb.get('type')}'",
                })
            else:
                # Check for other differences
                sub_changes = []

                if fa.get("format") != fb.get("format"):
                    sub_changes.append(f"format: {fa.get('format')} → {fb.get('format')}")
                if fa.get("required") != fb.get("required"):
                    if fb.get("required") and not fa.get("required"):
                        sub_changes.append("became required")
                        sev = SEVERITY_BREAKING
                    else:
                        sub_changes.append("became optional")
                        sev = SEVERITY_ADDITIVE
                if fa.get("enum") != fb.get("enum"):
                    old_enum = set(fa.get("enum", []))
                    new_enum = set(fb.get("enum", []))
                    removed_vals = old_enum - new_enum
                    added_vals = new_enum - old_enum
                    if removed_vals:
                        sub_changes.append(f"enum values removed: {removed_vals}")
                    if added_vals:
                        sub_changes.append(f"enum values added: {added_vals}")
                if fa.get("default") != fb.get("default"):
                    sub_changes.append(f"default: {fa.get('default')} → {fb.get('default')}")

                if sub_changes:
                    sev = classify_change("modified", field)
                    # Override if we detected specific severity above
                    for sc in sub_changes:
                        if "became required" in sc:
                            sev = SEVERITY_BREAKING
                            break
                        if "enum values removed" in sc:
                            sev = SEVERITY_BREAKING
                            break

                    changes.append({
                        "field": field,
                        "change": "modified",
                        "severity": sev,
                        "old": fa,
                        "new": fb,
                        "detail": "; ".join(sub_changes),
                    })

    return changes


def diff_parameters(endpoint_a: Dict[str, Any], endpoint_b: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Compare parameters between two endpoint versions."""
    changes = []
    params_a = get_parameters_dict(endpoint_a)
    params_b = get_parameters_dict(endpoint_b)

    all_params = set(params_a.keys()) | set(params_b.keys())

    for param in sorted(all_params):
        in_a = param in params_a
        in_b = param in params_b

        if in_a and not in_b:
            changes.append({
                "parameter": param,
                "change": "removed",
                "severity": SEVERITY_BREAKING,
                "old": params_a[param],
                "new": None,
                "detail": f"Parameter '{param}' ({params_a[param].get('in', '?')}) was removed",
            })
        elif not in_a and in_b:
            is_required = params_b[param].get("required", False)
            sev = SEVERITY_BREAKING if is_required else SEVERITY_ADDITIVE
            changes.append({
                "parameter": param,
                "change": "added",
                "severity": sev,
                "old": None,
                "new": params_b[param],
                "detail": f"Parameter '{param}' ({params_b[param].get('in', '?')}) was added"
                          + (" (required)" if is_required else ""),
            })
        else:
            pa = params_a[param]
            pb = params_b[param]

            sub_changes = []
            if pa.get("in") != pb.get("in"):
                sub_changes.append(f"location: {pa.get('in')} → {pb.get('in')}")
            if pa.get("required") != pb.get("required"):
                sub_changes.append(f"required: {pa.get('required')} → {pb.get('required')}")

            # Schema changes
            schema_a = pa.get("schema", {})
            schema_b = pb.get("schema", {})
            if schema_a.get("type") != schema_b.get("type"):
                sub_changes.append(f"type: {schema_a.get('type')} → {schema_b.get('type')}")
            if schema_a.get("format") != schema_b.get("format"):
                sub_changes.append(f"format: {schema_a.get('format')} → {schema_b.get('format')}")
            if schema_a.get("enum") != schema_b.get("enum"):
                sub_changes.append(f"enum changed")
            if schema_a.get("default") != schema_b.get("default"):
                sub_changes.append(f"default: {schema_a.get('default')} → {schema_b.get('default')}")

            if sub_changes:
                changes.append({
                    "parameter": param,
                    "change": "modified",
                    "severity": SEVERITY_MODIFIED,
                    "old": pa,
                    "new": pb,
                    "detail": "; ".join(sub_changes),
                })

    return changes


def diff_response_codes(endpoint_a: Dict[str, Any], endpoint_b: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Compare response status codes between two endpoint versions."""
    changes = []
    codes_a = set(endpoint_a.get("responses", {}).keys())
    codes_b = set(endpoint_b.get("responses", {}).keys())

    for code in sorted(codes_b - codes_a):
        changes.append({
            "status_code": code,
            "change": "added",
            "severity": SEVERITY_ADDITIVE,
            "detail": f"Response code {code} was added",
        })
    for code in sorted(codes_a - codes_b):
        changes.append({
            "status_code": code,
            "change": "removed",
            "severity": SEVERITY_BREAKING,
            "detail": f"Response code {code} was removed",
        })

    return changes


def full_endpoint_diff(endpoint_a: Dict[str, Any], endpoint_b: Dict[str, Any]) -> Dict[str, Any]:
    """
    Full comparison of a single endpoint across two versions.
    Compares parameters, request schema, response schema, and status codes.
    """
    req_schema_a = get_request_schema(endpoint_a)
    req_schema_b = get_request_schema(endpoint_b)
    resp_schema_a = get_response_schema(endpoint_a)
    resp_schema_b = get_response_schema(endpoint_b)

    param_changes = diff_parameters(endpoint_a, endpoint_b)
    req_changes = diff_flat_schemas(req_schema_a, req_schema_b)
    resp_changes = diff_flat_schemas(resp_schema_a, resp_schema_b)
    code_changes = diff_response_codes(endpoint_a, endpoint_b)

    all_changes = param_changes + req_changes + resp_changes + code_changes
    has_breaking = any(c.get("severity") == SEVERITY_BREAKING for c in all_changes)

    return {
        "parameter_changes": param_changes,
        "request_schema_changes": req_changes,
        "response_schema_changes": resp_changes,
        "response_code_changes": code_changes,
        "has_breaking_changes": has_breaking,
        "total_changes": len(all_changes),
    }
