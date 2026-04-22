"""
curl_runner.py — Execute curl commands and convert them to Python requests.

Handles:
  - Executing a token-fetch curl (returns raw response text)
  - Parsing the curl string into method/url/headers/body
  - Executing the patched API curl and returning structured results
"""

import re
import subprocess
import shlex
import time
from typing import Dict, Any, Optional, Tuple


# ─── Curl string → structured parts ──────────────────────────────────────────

def parse_curl(curl_str: str) -> Dict[str, Any]:
    """
    Parse a curl command string into its components.
    Returns dict with: method, url, headers, body, raw_curl
    """
    # Normalise line continuations and newlines
    curl_str = re.sub(r"\\\s*\n\s*", " ", curl_str).strip()

    result: Dict[str, Any] = {
        "method": "GET",
        "url": "",
        "headers": {},
        "body": None,
        "raw_curl": curl_str,
    }

    # Extract URL — first quoted or unquoted token after 'curl' flags
    url_match = re.search(r"curl\b[^'\"]*?['\"]?(https?://[^\s'\"\\]+)['\"]?", curl_str, re.IGNORECASE)
    if not url_match:
        url_match = re.search(r"['\"]?(https?://[^\s'\"]+)['\"]?", curl_str)
    if url_match:
        result["url"] = url_match.group(1).strip("'\"")

    # Method
    method_match = re.search(r"-X\s+([A-Z]+)", curl_str, re.IGNORECASE)
    if method_match:
        result["method"] = method_match.group(1).upper()
    elif re.search(r"--data|--data-raw|--data-binary|-d\b", curl_str):
        result["method"] = "POST"

    # Headers
    for hdr_match in re.finditer(r"-H\s+['\"]([^'\"]+)['\"]", curl_str):
        header_line = hdr_match.group(1)
        if ":" in header_line:
            k, _, v = header_line.partition(":")
            result["headers"][k.strip()] = v.strip()

    # Body / data
    body_match = re.search(
        r"(?:--data-raw|--data-binary|--data|-d)\s+['\"](.+?)['\"](?:\s|$)",
        curl_str,
        re.DOTALL
    )
    if body_match:
        result["body"] = body_match.group(1)

    return result


# ─── Execute via subprocess (real curl) ───────────────────────────────────────

def _run_curl_subprocess(curl_str: str, timeout: int = 30) -> Tuple[str, int, float]:
    """
    Run curl command via subprocess.
    Returns (stdout_text, returncode, elapsed_ms).
    """
    # Normalise line continuations
    curl_str = re.sub(r"\\\s*\n\s*", " ", curl_str).strip()

    # Ensure -s (silent) flag to suppress progress meter
    if " -s " not in curl_str and not curl_str.startswith("curl -s"):
        curl_str = curl_str.replace("curl ", "curl -s ", 1)

    start = time.perf_counter()
    try:
        proc = subprocess.run(
            curl_str,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        return proc.stdout, proc.returncode, elapsed_ms
    except subprocess.TimeoutExpired:
        elapsed_ms = timeout * 1000
        return "", -1, elapsed_ms
    except Exception as e:
        return str(e), -2, 0.0


# ─── Execute via Python requests (fallback / alternative) ─────────────────────

def _run_curl_requests(parsed: Dict[str, Any], timeout: int = 30) -> Tuple[str, int, float]:
    """Execute using Python requests library."""
    import requests as req

    method  = parsed["method"]
    url     = parsed["url"]
    headers = parsed["headers"]
    body    = parsed["body"]

    if not url:
        return "No URL found in curl command", -3, 0.0

    start = time.perf_counter()
    try:
        resp = req.request(
            method=method,
            url=url,
            headers=headers,
            data=body,
            timeout=timeout,
            verify=False,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        try:
            return resp.text, resp.status_code, elapsed_ms
        except Exception:
            return resp.text, resp.status_code, elapsed_ms
    except Exception as e:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return str(e), -4, elapsed_ms


# ─── Public API ───────────────────────────────────────────────────────────────

def fetch_token_from_curl(token_curl: str, timeout: int = 30) -> Dict[str, Any]:
    """
    Execute the token-fetch curl command and return:
      { "raw_response": str, "token": str|None, "error": str|None, "elapsed_ms": float }
    """
    from curl_processor.excel_loader import parse_bearer_token_from_response

    raw, rc, elapsed = _run_curl_subprocess(token_curl, timeout=timeout)

    token = None
    error = None

    if rc not in (0, 200):
        # Might still have a token even if curl returns non-zero
        token = parse_bearer_token_from_response(raw) if raw else None
        if not token:
            error = f"curl exited with code {rc}" + (f": {raw[:200]}" if raw else "")
    else:
        token = parse_bearer_token_from_response(raw)
        if not token:
            error = "Could not extract token from curl response"

    return {
        "raw_response": raw,
        "token": token,
        "error": error,
        "elapsed_ms": elapsed,
        "return_code": rc,
    }


def execute_api_curl(curl_str: str, timeout: int = 30) -> Dict[str, Any]:
    """
    Execute an API curl command (already patched with real token).
    Returns:
      { status_code, body, headers_out, elapsed_ms, error, raw_response }
    """
    import json as _json

    raw, rc, elapsed = _run_curl_subprocess(
        # Add -i to get response headers, -w for status code
        curl_str.replace("curl ", "curl -s -w '\\n__STATUS__%{http_code}' ", 1),
        timeout=timeout,
    )

    # Extract injected status code marker
    status_code = None
    body_text = raw
    if "__STATUS__" in raw:
        parts = raw.rsplit("__STATUS__", 1)
        body_text = parts[0].strip()
        try:
            status_code = int(parts[1].strip())
        except ValueError:
            status_code = rc

    # Try JSON parse
    body = None
    try:
        body = _json.loads(body_text)
    except Exception:
        body = body_text

    error = None
    if rc not in (0,) and not status_code:
        error = f"curl failed (code {rc})"

    return {
        "status_code": status_code or rc,
        "body": body,
        "raw_response": body_text,
        "elapsed_ms": round(elapsed, 1),
        "error": error,
    }
