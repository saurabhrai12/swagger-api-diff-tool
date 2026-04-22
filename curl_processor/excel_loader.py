"""
excel_loader.py — Parse the Excel/CSV file and extract structured rows.

Expected columns (case-insensitive):
  API NAME  — endpoint label e.g. "GET /accounts/v3/account/{accountId}/controls"
  CURLs     — multiline curl command(s) with placeholder bearer token
  OWNER     — "<Owner Name> token: curl ..." where the curl fetches the real token
"""

import re
import pandas as pd
from typing import Optional


# ─── Token extraction ─────────────────────────────────────────────────────────

def extract_token_curl(owner_cell: str) -> Optional[str]:
    """
    Given an OWNER cell value like:
        "John Doe token: curl -X POST 'https://auth.example.com/token' -d '...'"
    Return the curl command that will fetch the token (everything after 'token:').
    Returns None if 'token:' keyword not found.
    """
    if not isinstance(owner_cell, str):
        return None
    match = re.search(r"token\s*:\s*(curl\b.+)", owner_cell, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def extract_owner_name(owner_cell: str) -> str:
    """Extract just the owner name (text before 'token:')."""
    if not isinstance(owner_cell, str):
        return str(owner_cell)
    match = re.search(r"^(.*?)\s*token\s*:", owner_cell, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return owner_cell.strip()


# ─── Token parsing from curl response ─────────────────────────────────────────

def parse_bearer_token_from_response(response_text: str) -> Optional[str]:
    """
    Try to extract a bearer/access token from a curl auth response.
    Supports common formats:
      - {"access_token": "..."}
      - {"token": "..."}
      - {"id_token": "..."}
      - Plain text if the response itself is the token (no JSON)
    """
    import json

    response_text = response_text.strip()

    # Try JSON
    try:
        data = json.loads(response_text)
        for key in ("access_token", "token", "id_token", "bearer_token", "jwt"):
            if key in data and isinstance(data[key], str):
                return data[key]
        # Nested: data.data.access_token
        if isinstance(data.get("data"), dict):
            for key in ("access_token", "token", "id_token"):
                if key in data["data"]:
                    return data["data"][key]
    except (json.JSONDecodeError, AttributeError):
        pass

    # Regex fallback — look for a JWT-like or long alphanumeric string
    jwt_match = re.search(r"eyJ[\w\-]+\.[\w\-]+\.[\w\-]+", response_text)
    if jwt_match:
        return jwt_match.group(0)

    # If the whole response looks like a plain token (no spaces, reasonably long)
    if re.match(r"^[\w\-\.]{20,}$", response_text):
        return response_text

    return None


# ─── CURL patching ─────────────────────────────────────────────────────────────

BEARER_PATTERN = re.compile(
    r"(authorization\s*:\s*Bearer\s+)([^\s'\"\\\n]+)",
    re.IGNORECASE
)


def patch_bearer_token(curl_command: str, real_token: str) -> str:
    """Replace any 'authorization: Bearer <old>' with real_token in a curl string."""
    return BEARER_PATTERN.sub(lambda m: m.group(1) + real_token, curl_command)


# ─── Excel / CSV loading ──────────────────────────────────────────────────────

COLUMN_ALIASES = {
    "api name":  ["api name", "api_name", "apiname", "endpoint", "api"],
    "curls":     ["curls", "curl", "curl command", "curl_command", "command"],
    "owner":     ["owner", "api owner", "team", "api_owner"],
}


def _normalize_col(df: pd.DataFrame, canonical: str) -> Optional[str]:
    """Find the real DataFrame column name matching a canonical alias list."""
    aliases = COLUMN_ALIASES[canonical]
    for col in df.columns:
        if col.strip().lower() in aliases:
            return col
    return None


def load_excel(file_obj) -> pd.DataFrame:
    """
    Load an uploaded Excel or CSV file and return a normalised DataFrame with
    columns: api_name, curls, owner, owner_name, token_curl

    Raises ValueError with a helpful message if required columns are missing.
    """
    name = getattr(file_obj, "name", "")
    if name.endswith(".csv"):
        df = pd.read_csv(file_obj)
    else:
        df = pd.read_excel(file_obj)

    # Strip column whitespace
    df.columns = [c.strip() for c in df.columns]

    col_api   = _normalize_col(df, "api name")
    col_curls = _normalize_col(df, "curls")
    col_owner = _normalize_col(df, "owner")

    missing = [k for k, v in {"API NAME": col_api, "CURLs": col_curls, "OWNER": col_owner}.items() if v is None]
    if missing:
        raise ValueError(
            f"Missing required column(s): {', '.join(missing)}.\n"
            f"Found columns: {', '.join(df.columns)}"
        )

    out = pd.DataFrame({
        "api_name":   df[col_api].fillna("").astype(str).str.strip(),
        "curls":      df[col_curls].fillna("").astype(str).str.strip(),
        "owner":      df[col_owner].fillna("").astype(str).str.strip(),
        "owner_name": df[col_owner].apply(extract_owner_name),
        "token_curl": df[col_owner].apply(extract_token_curl),
    })

    return out
