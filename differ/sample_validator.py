"""
Validates sample inputs against each version's request schema.
Identifies which samples are compatible with which versions.
"""

from typing import Dict, Any, List, Optional
from jsonschema import validate, ValidationError, Draft7Validator
from parser.normalizer import extract_endpoints, get_request_schema, get_parameters_dict


def validate_sample_against_schema(sample_body: Dict[str, Any],
                                    schema: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a sample request body against a schema."""
    if not schema:
        return {"valid": True, "errors": []}

    errors = []
    v = Draft7Validator(schema)
    for error in sorted(v.iter_errors(sample_body), key=lambda e: list(e.absolute_path)):
        errors.append({
            "path": ".".join(str(p) for p in error.absolute_path) or "(root)",
            "message": error.message,
            "schema_path": ".".join(str(p) for p in error.schema_path),
        })

    return {
        "valid": len(errors) == 0,
        "errors": errors,
    }


def validate_sample_params(sample_params: Dict[str, Any],
                           endpoint: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Check if sample query/path params are valid for the endpoint."""
    warnings = []
    params_dict = get_parameters_dict(endpoint)

    # Check for params in sample that don't exist in spec
    for param_name in sample_params:
        if param_name not in params_dict:
            warnings.append({
                "param": param_name,
                "issue": "unknown_param",
                "message": f"Parameter '{param_name}' not found in spec",
            })

    # Check for required params missing from sample
    for param_name, param_info in params_dict.items():
        if param_info.get("required") and param_name not in sample_params:
            warnings.append({
                "param": param_name,
                "issue": "missing_required",
                "message": f"Required parameter '{param_name}' missing from sample",
            })

    return warnings


def validate_samples_across_versions(
    samples: Dict[str, Any],
    specs: Dict[str, Dict[str, Any]]
) -> Dict[str, Dict[str, Any]]:
    """
    Validate all samples against all versions.
    Returns a matrix of endpoint × version → validation result.
    """
    results = {}

    for endpoint_key, sample_data in samples.items():
        results[endpoint_key] = {}

        for version, spec in specs.items():
            endpoints = extract_endpoints(spec)

            if endpoint_key not in endpoints:
                results[endpoint_key][version] = {
                    "status": "NOT_FOUND",
                    "message": f"Endpoint not found in {version}",
                    "body_validation": None,
                    "param_warnings": [],
                }
                continue

            endpoint = endpoints[endpoint_key]
            result = {"status": "OK", "message": "", "body_validation": None, "param_warnings": []}

            # Validate request body
            if "body" in sample_data:
                req_schema = get_request_schema(endpoint)
                if req_schema:
                    body_result = validate_sample_against_schema(sample_data["body"], req_schema)
                    result["body_validation"] = body_result
                    if not body_result["valid"]:
                        result["status"] = "INVALID"
                        result["message"] = f"{len(body_result['errors'])} validation error(s)"

            # Validate params
            all_params = {}
            all_params.update(sample_data.get("path_params", {}))
            all_params.update(sample_data.get("query_params", {}))
            if all_params:
                param_warnings = validate_sample_params(all_params, endpoint)
                result["param_warnings"] = param_warnings
                if param_warnings:
                    if result["status"] == "OK":
                        result["status"] = "WARNING"
                    result["message"] += f" {len(param_warnings)} param warning(s)"

            if result["status"] == "OK":
                result["message"] = "Sample is valid"

            results[endpoint_key][version] = result

    return results
