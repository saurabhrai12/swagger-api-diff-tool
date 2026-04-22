"""
Reconciles spec-level diffs with live response diffs.
Identifies where spec and reality agree or disagree.
"""

from typing import Dict, Any, List


def reconcile(spec_diff: Dict[str, Any], response_diff: Dict[str, Any],
              version_a: str, version_b: str) -> List[Dict[str, Any]]:
    """
    Cross-reference spec changes with actual response differences.
    Returns a list of reconciliation findings.
    """
    findings = []

    # Get sets of changed fields from spec
    spec_resp_changes = spec_diff.get("response_schema_changes", [])
    spec_fields_added = {c["field"] for c in spec_resp_changes if c["change"] == "added"}
    spec_fields_removed = {c["field"] for c in spec_resp_changes if c["change"] == "removed"}
    spec_fields_modified = {c["field"] for c in spec_resp_changes if c["change"] in ("modified", "type_changed")}

    # Get sets of changed fields from live response
    body_diffs = response_diff.get("body_diffs", [])
    struct_diffs = response_diff.get("structure_diffs", [])

    live_fields_added = set()
    live_fields_removed = set()
    live_fields_changed = set()

    for d in body_diffs:
        path = d.get("path", "")
        change = d.get("change", "")
        if change == "field_added":
            live_fields_added.add(path)
        elif change == "field_removed":
            live_fields_removed.add(path)
        elif change in ("value_changed", "type_changed"):
            live_fields_changed.add(path)

    for d in struct_diffs:
        path = d.get("path", "")
        change = d.get("change", "")
        if change == "key_added":
            live_fields_added.add(path)
        elif change == "key_removed":
            live_fields_removed.add(path)

    # Reconciliation logic

    # 1. Fields spec says were added — check if actually present
    for field in spec_fields_added:
        if _field_in_set(field, live_fields_added):
            findings.append({
                "field": field,
                "spec_says": "added",
                "reality": "CONFIRMED",
                "status": "MATCH",
                "detail": f"Spec says '{field}' was added in {version_b}, confirmed in live response",
            })
        else:
            findings.append({
                "field": field,
                "spec_says": "added",
                "reality": "NOT_SEEN",
                "status": "UNCONFIRMED",
                "detail": f"Spec says '{field}' was added in {version_b}, but not observed in live response "
                          f"(may need different input to trigger)",
            })

    # 2. Fields spec says were removed — check if actually gone
    for field in spec_fields_removed:
        if _field_in_set(field, live_fields_removed):
            findings.append({
                "field": field,
                "spec_says": "removed",
                "reality": "CONFIRMED",
                "status": "MATCH",
                "detail": f"Spec says '{field}' was removed in {version_b}, confirmed absent in live response",
            })
        elif _field_in_set(field, live_fields_added) or _field_in_set(field, live_fields_changed):
            findings.append({
                "field": field,
                "spec_says": "removed",
                "reality": "STILL_PRESENT",
                "status": "MISMATCH",
                "detail": f"Spec says '{field}' was removed in {version_b}, but it's still in the live response!",
            })

    # 3. Fields changed in live but NOT in spec — spec is outdated
    all_spec_fields = spec_fields_added | spec_fields_removed | spec_fields_modified
    undocumented_changes = (live_fields_added | live_fields_removed | live_fields_changed) - all_spec_fields

    # Filter out DeepDiff noise (paths with $ prefix)
    undocumented_changes = {f for f in undocumented_changes if not f.startswith("$")}

    for field in sorted(undocumented_changes):
        change_type = "unknown"
        if field in live_fields_added:
            change_type = "present in new version"
        elif field in live_fields_removed:
            change_type = "missing in new version"
        elif field in live_fields_changed:
            change_type = "value differs"

        findings.append({
            "field": field,
            "spec_says": "no change",
            "reality": change_type,
            "status": "SPEC_OUTDATED",
            "detail": f"Field '{field}' has changed in live response ({change_type}) but spec doesn't mention it",
        })

    # 4. Status code reconciliation
    status_diff = response_diff.get("status_code_diff")
    if status_diff:
        spec_code_changes = spec_diff.get("response_code_changes", [])
        spec_codes_changed = {c["status_code"] for c in spec_code_changes}
        old_code = str(status_diff.get(version_a))
        new_code = str(status_diff.get(version_b))

        findings.append({
            "field": "(status_code)",
            "spec_says": f"codes changed: {spec_codes_changed}" if spec_codes_changed else "no status change",
            "reality": f"{old_code} → {new_code}",
            "status": "MATCH" if spec_codes_changed else "SPEC_OUTDATED",
            "detail": f"Status code changed from {old_code} to {new_code}",
        })

    if not findings:
        findings.append({
            "field": "(overall)",
            "spec_says": "changes documented",
            "reality": "no live differences detected",
            "status": "OK",
            "detail": "Spec and live responses are consistent (or endpoints not reachable for comparison)",
        })

    return findings


def _field_in_set(field: str, field_set: set) -> bool:
    """Check if a field matches any path in the set (fuzzy matching for nested paths)."""
    if field in field_set:
        return True
    # Try partial matches for nested paths
    for f in field_set:
        if field in f or f in field:
            return True
    return False
