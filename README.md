# Swagger API Diff Tool

Compare API versions at spec level, validate samples, call live endpoints, reconcile spec vs reality, and bulk-run API tests from an Excel sheet with auto-fetched bearer tokens.

## Quick Start

```bash
cd swagger-api-diff-tool
pip install -r requirements.txt
streamlit run app.py
```

The app opens at `http://localhost:8501`.

## Project Structure

```
swagger-api-diff-tool/
├── app.py                         # Streamlit UI (7 tabs)
├── config.py                      # Base URLs, timeouts, auth config
├── requirements.txt
├── parser/
│   ├── loader.py                  # Load & resolve Swagger JSON/YAML ($ref resolution)
│   └── normalizer.py              # Extract endpoints, schemas, flatten to comparable dicts
├── caller/
│   ├── api_client.py              # HTTP client — call live endpoints across versions
│   └── snapshot_store.py          # Save/load response snapshots for offline analysis
├── differ/
│   ├── endpoint_diff.py           # Added/removed/common endpoint detection
│   ├── schema_diff.py             # Field-level request/response schema diff with severity
│   ├── response_diff.py           # Deep diff of actual live responses (DeepDiff-based)
│   ├── sample_validator.py        # Validate sample inputs against each version's schema
│   └── reconciler.py              # Cross-reference spec diffs with live response diffs
├── curl_processor/
│   ├── excel_loader.py            # Parse Excel/CSV, extract & patch bearer tokens
│   └── curl_runner.py             # Execute token-fetch + API curls, return structured results
├── specs/                         # Drop your Swagger/OpenAPI JSON or YAML files here
├── samples/
│   └── samples.json               # Sample inputs per endpoint
└── snapshots/                     # Auto-saved response snapshots
```

## How to Plug In Your APIs

1. **Add spec files**: Drop your Swagger/OpenAPI JSON or YAML files into `specs/`. Name them by version (e.g., `v1.json`, `v2.yaml`).

2. **Update base URLs**: Edit `config.py` → `BASE_URLS`, or override in the sidebar at runtime.

3. **Add sample inputs**: Edit `samples/samples.json`:
   ```json
   {
     "GET /your/endpoint": { "query_params": { "page": 1 } },
     "POST /your/endpoint": { "body": { "field": "value" } },
     "GET /your/endpoint/{id}": { "path_params": { "id": "123" } }
   }
   ```

4. **Configure auth** (if needed): Edit `config.py` → `AUTH_CONFIG`.

## The 7 Tabs

| Tab | What It Does |
|-----|-------------|
| **📊 Overview** | Endpoint counts, availability matrix across all versions |
| **📋 Spec Diff** | Added/removed endpoints, parameter & schema changes with severity |
| **🔍 Schema Detail** | Deep dive into any schema — tree view, flattened fields, cross-version comparison |
| **✅ Sample Validation** | Matrix showing which samples pass/fail against which versions |
| **🌐 Live Response Diff** | Call real endpoints, side-by-side response comparison with deep diff |
| **🔗 Reconciliation** | Cross-references spec changes with actual response differences |
| **📂 Excel CURL Runner** | Upload Excel with API curls, auto-fetch bearer tokens per owner, bulk-run APIs |

## Excel CURL Runner

Upload an `.xlsx` or `.csv` file with these columns:

| Column | Description |
|--------|-------------|
| `API NAME` | Endpoint label (e.g. `GET /accounts/v3/account/{accountId}/controls`) |
| `CURLs` | curl command with placeholder bearer token (`authorization: Bearer <placeholder>`) |
| `OWNER` | Owner name followed by `token: curl ...` — the curl that fetches the real token |

**OWNER column example:**
```
John Doe token: curl -X POST 'https://auth.example.com/oauth/token' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'client_id=xxx&client_secret=yyy&grant_type=client_credentials'
```

The tool will:
1. Extract and execute the token curl for each unique owner
2. Parse the real bearer token from the JSON response (`access_token`, `token`, or `id_token`)
3. Replace the placeholder in the `CURLs` column with the real token
4. Execute all patched API curls and show results (status, body, timing)
5. Allow exporting results to Excel

> ⚠️ **Security**: Never commit your Excel files — they contain tokens. They are excluded from git via `.gitignore`.

## Severity Levels

- 🔴 **BREAKING** — Field removed, type changed, required field added, enum values removed
- 🟡 **MODIFIED** — Default changed, format changed, description changed
- 🟢 **ADDITIVE** — New optional field, new endpoint, new enum value
- ⚪ **COSMETIC** — Whitespace, ordering, description-only changes

## Security Notes

- Bearer tokens fetched at runtime are held in Streamlit session state only — never written to disk
- Spec files in `specs/` are git-ignored by default; add individual files back with `git add -f specs/myfile.json` if they are safe to share
- Snapshots are git-ignored (may contain live response data)
