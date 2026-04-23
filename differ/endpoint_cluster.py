"""
endpoint_cluster.py — Intelligent endpoint similarity engine.

Provides two high-level analyses:
  1. find_merge_candidates  — cross-version: which v1 endpoints were likely
                              merged into which v2 endpoint?
  2. find_redundant_endpoints — same-version: which endpoints are suspiciously
                               similar (duplication / consolidation candidates)?

Similarity is computed from four weighted signals:
  - Path token Jaccard  (35%)
  - Parameter overlap   (25%)
  - Request schema      (20%)
  - Response schema     (20%)

HTTP method mismatch applies a 0.5× penalty.
"""

from __future__ import annotations

import re
from typing import Dict, List, Any, Tuple, Set

from parser.normalizer import (
    flatten_schema,
    get_request_schema,
    get_response_schema,
    get_parameters_dict,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _path_tokens(path_key: str) -> Set[str]:
    """
    Extract meaningful tokens from an endpoint key like 'GET /accounts/{id}/controls'.
    Strips path parameters ({...}), HTTP method, and splits on '/'.
    """
    # Remove method prefix
    path = re.sub(r"^[A-Z]+\s+", "", path_key)
    # Remove path parameters
    path = re.sub(r"\{[^}]+\}", "", path)
    tokens = {t.lower() for t in re.split(r"[/\-_.]", path) if t.strip()}
    return tokens


def _param_keys(endpoint: Dict) -> Set[str]:
    """Return set of 'name' keys for all parameters."""
    return {p.get("name", "").lower() for p in endpoint.get("parameters", []) if p.get("name")}


def _req_field_keys(endpoint: Dict) -> Set[str]:
    schema = get_request_schema(endpoint)
    if not schema:
        return set()
    return set(flatten_schema(schema).keys())


def _resp_field_keys(endpoint: Dict) -> Set[str]:
    """Union of all 2xx response schema field names."""
    fields: Set[str] = set()
    for code in endpoint.get("responses", {}):
        if str(code).startswith("2"):
            schema = get_response_schema(endpoint, str(code))
            if schema:
                fields.update(flatten_schema(schema).keys())
    return fields


def _jaccard(a: Set, b: Set) -> float:
    if not a and not b:
        return 1.0   # both empty → identical in this dimension
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _method(key: str) -> str:
    m = re.match(r"^([A-Z]+)\s+", key)
    return m.group(1).upper() if m else ""


# ─── Core similarity ──────────────────────────────────────────────────────────

def score_pair(
    key1: str, ep1: Dict,
    key2: str, ep2: Dict,
) -> Tuple[float, Dict[str, float]]:
    """
    Compute similarity (0–100) between two endpoints.

    Returns (overall_score, breakdown_dict).
    breakdown_dict keys: path, params, req_schema, resp_schema.
    """
    path_score   = _jaccard(_path_tokens(key1), _path_tokens(key2)) * 100
    param_score  = _jaccard(_param_keys(ep1),   _param_keys(ep2))   * 100
    req_score    = _jaccard(_req_field_keys(ep1), _req_field_keys(ep2)) * 100
    resp_score   = _jaccard(_resp_field_keys(ep1), _resp_field_keys(ep2)) * 100

    weighted = (
        path_score  * 0.35 +
        param_score * 0.25 +
        req_score   * 0.20 +
        resp_score  * 0.20
    )

    # Method mismatch penalty
    if _method(key1) and _method(key2) and _method(key1) != _method(key2):
        weighted *= 0.5

    breakdown = {
        "path":       round(path_score,  1),
        "params":     round(param_score, 1),
        "req_schema": round(req_score,   1),
        "resp_schema": round(resp_score, 1),
    }
    return round(weighted, 1), breakdown


# ─── Cross-version: Merge detection ──────────────────────────────────────────

def find_merge_candidates(
    src_eps: Dict[str, Dict],   # v1 endpoints {key: ep_dict}
    tgt_eps: Dict[str, Dict],   # v2 endpoints {key: ep_dict}
    threshold: float = 40.0,
) -> List[Dict[str, Any]]:
    """
    For each v2 endpoint, find all v1 endpoints that score >= threshold.

    Returns a list of merge groups, sorted by number of v1 candidates desc:
    [
      {
        "v2_key": str,
        "candidates": [{"v1_key": str, "score": float, "breakdown": dict}, ...],
        "confidence": float,   # avg score of candidates
        "verdict": str,        # human-readable summary
      }, ...
    ]
    Only groups with ≥1 candidate are returned.
    """
    groups: List[Dict] = []

    for v2_key, v2_ep in tgt_eps.items():
        candidates = []
        for v1_key, v1_ep in src_eps.items():
            score, breakdown = score_pair(v1_key, v1_ep, v2_key, v2_ep)
            if score >= threshold:
                candidates.append({
                    "v1_key":    v1_key,
                    "score":     score,
                    "breakdown": breakdown,
                })

        if not candidates:
            continue

        candidates.sort(key=lambda x: x["score"], reverse=True)
        confidence = round(sum(c["score"] for c in candidates) / len(candidates), 1)

        n = len(candidates)
        if n == 1:
            verdict = f"Likely 1-to-1 replacement (score {candidates[0]['score']}%)"
        else:
            verdict = f"Likely merge of {n} v1 endpoints (avg confidence {confidence}%)"

        groups.append({
            "v2_key":     v2_key,
            "candidates": candidates,
            "confidence": confidence,
            "verdict":    verdict,
        })

    groups.sort(key=lambda g: (len(g["candidates"]), g["confidence"]), reverse=True)
    return groups


# ─── Same-version: Redundancy detection ──────────────────────────────────────

def find_redundant_endpoints(
    eps: Dict[str, Dict],
    threshold: float = 60.0,
) -> List[Dict[str, Any]]:
    """
    Find pairs of endpoints within the same version that are highly similar.

    Returns list of pairs sorted by score desc:
    [
      {
        "ep1_key": str, "ep2_key": str,
        "score": float,
        "breakdown": dict,
        "verdict": str,
      }, ...
    ]
    """
    keys = sorted(eps.keys())
    pairs: List[Dict] = []

    for i, k1 in enumerate(keys):
        for k2 in keys[i + 1:]:
            score, breakdown = score_pair(k1, eps[k1], k2, eps[k2])
            if score >= threshold:
                if score >= 85:
                    verdict = "🔴 Very high overlap — strong consolidation candidate"
                elif score >= 70:
                    verdict = "🟡 Significant overlap — review for redundancy"
                else:
                    verdict = "🟠 Moderate overlap — worth investigating"
                pairs.append({
                    "ep1_key":   k1,
                    "ep2_key":   k2,
                    "score":     score,
                    "breakdown": breakdown,
                    "verdict":   verdict,
                })

    pairs.sort(key=lambda x: x["score"], reverse=True)
    return pairs
