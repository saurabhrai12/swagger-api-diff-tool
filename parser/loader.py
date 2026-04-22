"""
Loads and resolves Swagger/OpenAPI spec files.
Handles both JSON and YAML, resolves all $ref references.
"""

import json
import os
import copy
from typing import Dict, Any, Optional


def load_spec(file_path: str) -> Dict[str, Any]:
    """Load a Swagger/OpenAPI spec from JSON or YAML file."""
    with open(file_path, "r") as f:
        if file_path.endswith((".yaml", ".yml")):
            import yaml
            spec = yaml.safe_load(f)
        else:
            spec = json.load(f)
    return spec


def resolve_ref(spec: Dict[str, Any], ref: str) -> Dict[str, Any]:
    """Resolve a $ref pointer within the spec."""
    if not ref.startswith("#/"):
        return {"$ref": ref}  # External refs not supported
    parts = ref[2:].split("/")
    current = spec
    for part in parts:
        part = part.replace("~1", "/").replace("~0", "~")
        current = current.get(part, {})
    return copy.deepcopy(current)


def resolve_all_refs(spec: Dict[str, Any], root: Optional[Dict[str, Any]] = None,
                     _seen: Optional[set] = None) -> Any:
    """Recursively resolve all $ref references in the spec."""
    if root is None:
        root = spec
    if _seen is None:
        _seen = set()

    if isinstance(spec, dict):
        if "$ref" in spec:
            ref = spec["$ref"]
            if ref in _seen:
                return {"$circular_ref": ref}
            _seen = _seen | {ref}
            resolved = resolve_ref(root, ref)
            return resolve_all_refs(resolved, root, _seen)
        return {k: resolve_all_refs(v, root, _seen) for k, v in spec.items()}
    elif isinstance(spec, list):
        return [resolve_all_refs(item, root, _seen) for item in spec]
    return spec


def load_and_resolve(file_path: str) -> Dict[str, Any]:
    """Load a spec file and resolve all internal $ref references."""
    spec = load_spec(file_path)
    return resolve_all_refs(spec)


def load_specs_from_dir(specs_dir: str) -> Dict[str, Dict[str, Any]]:
    """Load all spec files from a directory, keyed by version name."""
    specs = {}
    for fname in sorted(os.listdir(specs_dir)):
        if fname.endswith((".json", ".yaml", ".yml")):
            version = os.path.splitext(fname)[0]
            fpath = os.path.join(specs_dir, fname)
            try:
                specs[version] = load_and_resolve(fpath)
            except Exception as e:
                print(f"Warning: Failed to load {fname}: {e}")
    return specs
