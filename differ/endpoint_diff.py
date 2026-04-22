"""
Compares endpoints between two API versions.
Identifies added, removed, and modified endpoints.
"""

from typing import Dict, Any, List, Tuple
from parser.normalizer import extract_endpoints


def diff_endpoints(spec_a: Dict[str, Any], spec_b: Dict[str, Any],
                   version_a: str = "A", version_b: str = "B") -> Dict[str, Any]:
    """
    Compare endpoints between two spec versions.
    Returns a structured diff with added, removed, and common endpoints.
    """
    endpoints_a = extract_endpoints(spec_a)
    endpoints_b = extract_endpoints(spec_b)

    keys_a = set(endpoints_a.keys())
    keys_b = set(endpoints_b.keys())

    added = keys_b - keys_a
    removed = keys_a - keys_b
    common = keys_a & keys_b

    # Check for method changes on same path
    paths_a = {}
    paths_b = {}
    for key in keys_a:
        method, path = key.split(" ", 1)
        paths_a.setdefault(path, set()).add(method)
    for key in keys_b:
        method, path = key.split(" ", 1)
        paths_b.setdefault(path, set()).add(method)

    method_changes = []
    for path in set(paths_a.keys()) & set(paths_b.keys()):
        methods_added = paths_b[path] - paths_a[path]
        methods_removed = paths_a[path] - paths_b[path]
        if methods_added or methods_removed:
            method_changes.append({
                "path": path,
                "methods_added": sorted(methods_added),
                "methods_removed": sorted(methods_removed),
            })

    return {
        "version_a": version_a,
        "version_b": version_b,
        "added": {k: endpoints_b[k] for k in sorted(added)},
        "removed": {k: endpoints_a[k] for k in sorted(removed)},
        "common": sorted(common),
        "method_changes": method_changes,
        "summary": {
            "total_in_a": len(endpoints_a),
            "total_in_b": len(endpoints_b),
            "added_count": len(added),
            "removed_count": len(removed),
            "common_count": len(common),
        }
    }
