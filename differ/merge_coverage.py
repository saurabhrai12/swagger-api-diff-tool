"""
merge_coverage.py — Analyse how well a single v2 endpoint covers
multiple v1 endpoints that were merged/consolidated into it.

Coverage is measured across:
  - URL / query / header / cookie parameters
  - Request body schema fields
  - Response body schema fields (per status-code bucket)
  - Response status codes
"""

from typing import Dict, List, Any, Set, Tuple
from parser.normalizer import (
    flatten_schema,
    get_request_schema,
    get_response_schema,
    get_parameters_dict,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _param_key(param: Dict) -> str:
    """Unique key for a parameter: '<in>:<name>'"""
    return f"{param.get('in', 'query')}:{param.get('name', '')}"


def _flatten_params(endpoint: Dict) -> Dict[str, Dict]:
    """Return {param_key: param_dict} for an endpoint."""
    return {_param_key(p): p for p in endpoint.get("parameters", [])}


def _flatten_req_fields(endpoint: Dict) -> Dict[str, Dict]:
    """Flatten the request body schema into dotted field paths."""
    schema = get_request_schema(endpoint)
    if not schema:
        return {}
    return flatten_schema(schema)


def _flatten_resp_fields(endpoint: Dict, status_prefix: str = "2") -> Dict[str, Dict]:
    """
    Flatten all response body schemas whose status code starts with status_prefix.
    Returns union of all fields across matching status codes.
    """
    fields: Dict[str, Dict] = {}
    responses = endpoint.get("responses", {})
    for code, resp_obj in responses.items():
        if str(code).startswith(status_prefix):
            schema = get_response_schema(endpoint, str(code))
            if schema:
                fields.update(flatten_schema(schema))
    return fields


def _all_response_codes(endpoint: Dict) -> Set[str]:
    return set(str(k) for k in endpoint.get("responses", {}).keys())


# ─── Core coverage computation ────────────────────────────────────────────────

def compute_merge_coverage(
    v1_endpoints: Dict[str, Dict],   # { ep_key: endpoint_dict } for selected v1 eps
    v2_endpoint: Dict,               # single v2 endpoint dict
    v2_ep_key: str,
) -> Dict[str, Any]:
    """
    Compute coverage of v2_endpoint over all v1_endpoints combined.

    Returns a dict with sections:
      parameters, request_schema, response_schema, response_codes,
      summary (overall %)
    """

    # ── Aggregate v1 universe ──
    v1_params:    Dict[str, Dict] = {}
    v1_req_fields: Dict[str, Dict] = {}
    v1_resp_fields: Dict[str, Dict] = {}
    v1_resp_codes: Set[str] = set()
    v1_ep_param_map: Dict[str, Set[str]] = {}   # ep_key → set of param keys

    for ep_key, ep in v1_endpoints.items():
        ep_params = _flatten_params(ep)
        v1_params.update(ep_params)
        v1_ep_param_map[ep_key] = set(ep_params.keys())

        v1_req_fields.update(_flatten_req_fields(ep))
        v1_resp_fields.update(_flatten_resp_fields(ep))
        v1_resp_codes.update(_all_response_codes(ep))

    # ── v2 universe ──
    v2_params     = _flatten_params(v2_endpoint)
    v2_req_fields = _flatten_req_fields(v2_endpoint)
    v2_resp_fields = _flatten_resp_fields(v2_endpoint)
    v2_resp_codes  = _all_response_codes(v2_endpoint)

    # ── Parameters coverage ──
    param_rows = []
    for pk, pdict in sorted(v1_params.items()):
        in_v2 = pk in v2_params
        sources = [ep for ep, pset in v1_ep_param_map.items() if pk in pset]
        param_rows.append({
            "parameter":   pdict.get("name", pk),
            "in":          pdict.get("in", ""),
            "required_v1": pdict.get("required", False),
            "in_v2":       in_v2,
            "v2_required": v2_params.get(pk, {}).get("required", False) if in_v2 else None,
            "sources":     sources,
            "status":      "✅ Covered" if in_v2 else "❌ Missing",
        })

    # ── Request schema coverage ──
    req_rows = []
    for field, fdict in sorted(v1_req_fields.items()):
        in_v2 = field in v2_req_fields
        req_rows.append({
            "field":       field,
            "type_v1":     fdict.get("type", ""),
            "required_v1": fdict.get("required", False),
            "in_v2":       in_v2,
            "type_v2":     v2_req_fields.get(field, {}).get("type", "") if in_v2 else "",
            "status":      "✅ Covered" if in_v2 else "❌ Missing",
        })

    # ── Response schema coverage ──
    resp_rows = []
    for field, fdict in sorted(v1_resp_fields.items()):
        in_v2 = field in v2_resp_fields
        resp_rows.append({
            "field":   field,
            "type_v1": fdict.get("type", ""),
            "in_v2":   in_v2,
            "type_v2": v2_resp_fields.get(field, {}).get("type", "") if in_v2 else "",
            "status":  "✅ Covered" if in_v2 else "❌ Missing",
        })

    # ── Response codes ──
    all_codes = v1_resp_codes | v2_resp_codes
    code_rows = []
    for code in sorted(all_codes):
        in_v1 = code in v1_resp_codes
        in_v2 = code in v2_resp_codes
        if in_v1 and in_v2:
            status = "✅ Both"
        elif in_v1:
            status = "❌ Missing in v2"
        else:
            status = "🆕 New in v2"
        code_rows.append({"code": code, "in_v1": in_v1, "in_v2": in_v2, "status": status})

    # ── Coverage percentages ──
    def _pct(covered: int, total: int) -> float:
        return round(100 * covered / total, 1) if total else 100.0

    param_covered   = sum(1 for r in param_rows if r["in_v2"])
    req_covered     = sum(1 for r in req_rows if r["in_v2"])
    resp_covered    = sum(1 for r in resp_rows if r["in_v2"])
    codes_covered   = sum(1 for r in code_rows if r["in_v1"] and r["in_v2"])
    codes_v1_total  = sum(1 for r in code_rows if r["in_v1"])

    param_pct  = _pct(param_covered,  len(param_rows))
    req_pct    = _pct(req_covered,    len(req_rows))
    resp_pct   = _pct(resp_covered,   len(resp_rows))
    codes_pct  = _pct(codes_covered,  codes_v1_total)

    # Weighted overall (params 30%, req 20%, resp 30%, codes 20%)
    weights = [(param_pct, 0.30), (req_pct, 0.20), (resp_pct, 0.30), (codes_pct, 0.20)]
    overall_pct = round(sum(p * w for p, w in weights), 1)

    return {
        "v2_ep_key": v2_ep_key,
        "v1_ep_keys": list(v1_endpoints.keys()),
        "parameters": {
            "rows": param_rows,
            "covered": param_covered,
            "total": len(param_rows),
            "pct": param_pct,
        },
        "request_schema": {
            "rows": req_rows,
            "covered": req_covered,
            "total": len(req_rows),
            "pct": req_pct,
        },
        "response_schema": {
            "rows": resp_rows,
            "covered": resp_covered,
            "total": len(resp_rows),
            "pct": resp_pct,
        },
        "response_codes": {
            "rows": code_rows,
            "covered": codes_covered,
            "total": codes_v1_total,
            "pct": codes_pct,
        },
        "overall_pct": overall_pct,
    }


def coverage_color(pct: float) -> str:
    """Return a colour emoji for a coverage percentage."""
    if pct >= 90:
        return "🟢"
    if pct >= 60:
        return "🟡"
    return "🔴"


def per_endpoint_coverage(
    v1_endpoints: Dict[str, Dict],
    v2_endpoint: Dict,
) -> List[Dict]:
    """
    Return per-v1-endpoint breakdown showing what % of that specific endpoint
    is individually covered by v2.
    """
    rows = []
    for ep_key, ep in v1_endpoints.items():
        single = compute_merge_coverage({ep_key: ep}, v2_endpoint, "")
        rows.append({
            "v1_endpoint":   ep_key,
            "Params %":      single["parameters"]["pct"],
            "Req Schema %":  single["request_schema"]["pct"],
            "Resp Schema %": single["response_schema"]["pct"],
            "Codes %":       single["response_codes"]["pct"],
            "Overall %":     single["overall_pct"],
            "Grade":         coverage_color(single["overall_pct"]),
        })
    return rows
