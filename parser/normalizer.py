"""
Normalizes resolved OpenAPI specs into structured, comparable formats.
Extracts endpoints, schemas, parameters into flat dictionaries.
"""

from typing import Dict, Any, List, Optional, Tuple


def extract_endpoints(spec: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Extract all endpoints as a flat dict.
    Key: 'METHOD /path' (e.g., 'GET /users')
    Value: Full operation object with resolved schemas.
    """
    endpoints = {}
    paths = spec.get("paths", {})
    for path, path_item in paths.items():
        for method in ["get", "post", "put", "patch", "delete", "options", "head"]:
            if method in path_item:
                op = path_item[method]
                key = f"{method.upper()} {path}"
                endpoints[key] = {
                    "method": method.upper(),
                    "path": path,
                    "operation_id": op.get("operationId", ""),
                    "summary": op.get("summary", ""),
                    "description": op.get("description", ""),
                    "parameters": op.get("parameters", []),
                    "request_body": op.get("requestBody"),
                    "responses": op.get("responses", {}),
                    "deprecated": op.get("deprecated", False),
                    "tags": op.get("tags", []),
                }
    return endpoints


def extract_schemas(spec: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Extract component schemas."""
    return spec.get("components", {}).get("schemas", {})


def extract_server_url(spec: Dict[str, Any]) -> str:
    """Extract the first server URL from the spec."""
    servers = spec.get("servers", [])
    if servers:
        return servers[0].get("url", "")
    return ""


def flatten_schema(schema: Dict[str, Any], prefix: str = "") -> Dict[str, Dict[str, Any]]:
    """
    Flatten a nested schema into dot-notation field paths.
    Returns: {'field.subfield': {'type': 'string', 'required': True, ...}}
    """
    fields = {}
    if not isinstance(schema, dict):
        return fields

    schema_type = schema.get("type", "object")
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    for prop_name, prop_schema in properties.items():
        full_path = f"{prefix}.{prop_name}" if prefix else prop_name
        prop_type = prop_schema.get("type", "object")
        field_info = {
            "type": prop_type,
            "format": prop_schema.get("format"),
            "required": prop_name in required,
            "enum": prop_schema.get("enum"),
            "default": prop_schema.get("default"),
            "description": prop_schema.get("description"),
            "minimum": prop_schema.get("minimum"),
            "maximum": prop_schema.get("maximum"),
        }
        # Clean None values
        field_info = {k: v for k, v in field_info.items() if v is not None}
        fields[full_path] = field_info

        # Recurse into nested objects
        if prop_type == "object" and "properties" in prop_schema:
            nested = flatten_schema(prop_schema, full_path)
            fields.update(nested)
        elif prop_type == "array" and "items" in prop_schema:
            items = prop_schema["items"]
            if items.get("type") == "object" and "properties" in items:
                nested = flatten_schema(items, f"{full_path}[]")
                fields.update(nested)

    return fields


def get_request_schema(endpoint: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract the request body schema from an endpoint."""
    rb = endpoint.get("request_body")
    if not rb:
        return None
    content = rb.get("content", {})
    json_content = content.get("application/json", {})
    return json_content.get("schema")


def get_response_schema(endpoint: Dict[str, Any], status_code: str = "200") -> Optional[Dict[str, Any]]:
    """Extract response schema for a given status code."""
    responses = endpoint.get("responses", {})
    response = responses.get(status_code, responses.get("201", {}))
    content = response.get("content", {})
    json_content = content.get("application/json", {})
    return json_content.get("schema")


def get_parameters_dict(endpoint: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Convert parameter list to a dict keyed by name."""
    params = {}
    for p in endpoint.get("parameters", []):
        name = p.get("name", "")
        params[name] = {
            "in": p.get("in"),
            "required": p.get("required", False),
            "schema": p.get("schema", {}),
            "description": p.get("description"),
        }
    return params
