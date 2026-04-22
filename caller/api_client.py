"""
HTTP client for calling live API endpoints across versions.
Builds requests from Swagger specs + sample inputs, captures full responses.
"""

import time
import json
import httpx
from typing import Dict, Any, Optional, List
from parser.normalizer import extract_endpoints, extract_server_url


def build_url(base_url: str, path: str, path_params: Dict[str, str] = None) -> str:
    """Build the full URL with path parameters interpolated."""
    url = base_url.rstrip("/") + path
    if path_params:
        for param, value in path_params.items():
            url = url.replace(f"{{{param}}}", str(value))
    return url


def call_endpoint(
    base_url: str,
    method: str,
    path: str,
    path_params: Dict[str, str] = None,
    query_params: Dict[str, Any] = None,
    body: Dict[str, Any] = None,
    headers: Dict[str, str] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """
    Call a single API endpoint and capture the full response.
    Returns structured response data including timing.
    """
    url = build_url(base_url, path, path_params)

    default_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if headers:
        default_headers.update(headers)

    start_time = time.time()
    error = None
    response_data = None

    try:
        with httpx.Client(timeout=timeout, verify=False) as client:
            response = client.request(
                method=method,
                url=url,
                params=query_params,
                json=body if body and method in ("POST", "PUT", "PATCH") else None,
                headers=default_headers,
            )
            elapsed = time.time() - start_time

            # Try to parse JSON response
            try:
                resp_body = response.json()
            except Exception:
                resp_body = response.text

            response_data = {
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "body": resp_body,
                "response_time_ms": round(elapsed * 1000, 2),
                "url": str(response.url),
                "content_type": response.headers.get("content-type", ""),
            }

    except httpx.ConnectError as e:
        elapsed = time.time() - start_time
        error = f"Connection refused: {e}"
    except httpx.TimeoutException as e:
        elapsed = time.time() - start_time
        error = f"Request timed out after {timeout}s"
    except Exception as e:
        elapsed = time.time() - start_time
        error = f"Request failed: {type(e).__name__}: {e}"

    if error:
        response_data = {
            "status_code": None,
            "headers": {},
            "body": None,
            "response_time_ms": round(elapsed * 1000, 2),
            "url": build_url(base_url, path, path_params),
            "error": error,
        }

    return response_data


def call_across_versions(
    endpoint_key: str,
    sample: Dict[str, Any],
    specs: Dict[str, Dict[str, Any]],
    base_urls: Dict[str, str] = None,
    headers: Dict[str, str] = None,
    timeout: int = 30,
) -> Dict[str, Dict[str, Any]]:
    """
    Call the same logical endpoint across all API versions with the same sample input.
    Returns version → response data mapping.
    """
    results = {}
    parts = endpoint_key.split(" ", 1)
    method = parts[0]
    path = parts[1] if len(parts) > 1 else ""

    for version, spec in specs.items():
        # Determine base URL
        if base_urls and version in base_urls:
            base_url = base_urls[version]
        else:
            base_url = extract_server_url(spec)

        if not base_url:
            results[version] = {
                "status_code": None,
                "body": None,
                "error": f"No base URL configured for {version}",
                "url": "",
                "response_time_ms": 0,
            }
            continue

        # Check if endpoint exists in this version
        endpoints = extract_endpoints(spec)
        if endpoint_key not in endpoints:
            results[version] = {
                "status_code": None,
                "body": None,
                "error": f"Endpoint {endpoint_key} not found in {version}",
                "url": "",
                "response_time_ms": 0,
            }
            continue

        results[version] = call_endpoint(
            base_url=base_url,
            method=method,
            path=path,
            path_params=sample.get("path_params"),
            query_params=sample.get("query_params"),
            body=sample.get("body"),
            headers=headers,
            timeout=timeout,
        )

    return results
