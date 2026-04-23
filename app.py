"""
Swagger API Diff Tool — Streamlit Application
Compare API versions at spec level, validate samples, call live endpoints,
and reconcile spec vs reality.
"""

import streamlit as st
import json
import os
import sys
import pandas as pd
from typing import Dict, Any

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parser.loader import load_and_resolve, load_specs_from_dir
from parser.normalizer import (
    extract_endpoints, extract_schemas, flatten_schema,
    get_request_schema, get_response_schema, get_parameters_dict
)
from differ.endpoint_diff import diff_endpoints
from differ.schema_diff import full_endpoint_diff, SEVERITY_BREAKING, SEVERITY_ADDITIVE, SEVERITY_MODIFIED
from differ.sample_validator import validate_samples_across_versions
from differ.response_diff import diff_responses
from differ.reconciler import reconcile
from caller.api_client import call_across_versions
from caller.snapshot_store import save_snapshot, load_snapshot, list_snapshots
from config import SPECS_DIR, SAMPLES_DIR, SNAPSHOTS_DIR, BASE_URLS, REQUEST_TIMEOUT
from curl_processor.excel_loader import load_excel, patch_bearer_token
from curl_processor.curl_runner import fetch_token_from_curl, execute_api_curl
from differ.merge_coverage import compute_merge_coverage, per_endpoint_coverage, coverage_color
from differ.endpoint_cluster import find_merge_candidates, find_redundant_endpoints, score_pair

# ─── Page Config ───
st.set_page_config(
    page_title="API Version Diff Tool",
    page_icon="🔀",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Custom CSS ───
st.markdown("""
<style>
    .severity-breaking { background-color: #ff4b4b22; border-left: 4px solid #ff4b4b; padding: 8px; margin: 4px 0; border-radius: 4px; }
    .severity-additive { background-color: #21c35422; border-left: 4px solid #21c354; padding: 8px; margin: 4px 0; border-radius: 4px; }
    .severity-modified { background-color: #faca1522; border-left: 4px solid #faca15; padding: 8px; margin: 4px 0; border-radius: 4px; }
    .severity-cosmetic { background-color: #80808022; border-left: 4px solid #808080; padding: 8px; margin: 4px 0; border-radius: 4px; }
    .metric-card { border: 1px solid #ddd; border-radius: 8px; padding: 16px; text-align: center; }
    .stTabs [data-baseweb="tab-list"] { gap: 8px; }
    .stTabs [data-baseweb="tab"] { padding: 8px 16px; }
    div[data-testid="stExpander"] { border: 1px solid #e0e0e0; border-radius: 8px; margin-bottom: 8px; }
</style>
""", unsafe_allow_html=True)


def severity_badge(severity: str) -> str:
    """Return a colored badge for severity level."""
    colors = {
        SEVERITY_BREAKING: "🔴",
        SEVERITY_ADDITIVE: "🟢",
        SEVERITY_MODIFIED: "🟡",
        "COSMETIC": "⚪",
    }
    return f"{colors.get(severity, '⚪')} **{severity}**"


def severity_html(severity: str, text: str) -> str:
    """Wrap text in severity-colored div."""
    cls = f"severity-{severity.lower()}"
    return f'<div class="{cls}">{text}</div>'


# ─── Sidebar: Load Specs ───
st.sidebar.title("⚙️ Configuration")

# Load specs
specs = {}
spec_source = st.sidebar.radio("Spec Source", ["From specs/ directory", "Upload files"])

if spec_source == "From specs/ directory":
    if os.path.exists(SPECS_DIR):
        specs = load_specs_from_dir(SPECS_DIR)
        if specs:
            st.sidebar.success(f"Loaded {len(specs)} specs: {', '.join(specs.keys())}")
        else:
            st.sidebar.warning("No spec files found in specs/ directory")
    else:
        st.sidebar.error(f"Specs directory not found: {SPECS_DIR}")
else:
    uploaded_files = st.sidebar.file_uploader(
        "Upload Swagger/OpenAPI specs",
        type=["json", "yaml", "yml"],
        accept_multiple_files=True
    )
    if uploaded_files:
        for uf in uploaded_files:
            version = os.path.splitext(uf.name)[0]
            content = uf.read().decode("utf-8")
            if uf.name.endswith((".yaml", ".yml")):
                import yaml
                raw = yaml.safe_load(content)
            else:
                raw = json.loads(content)
            from parser.loader import resolve_all_refs
            specs[version] = resolve_all_refs(raw)
        st.sidebar.success(f"Loaded {len(specs)} uploaded specs")

# Load samples
samples = {}
samples_file = os.path.join(SAMPLES_DIR, "samples.json")
if os.path.exists(samples_file):
    with open(samples_file) as f:
        samples = json.load(f)
    st.sidebar.info(f"Loaded {len(samples)} sample inputs")

# Base URL overrides
st.sidebar.subheader("Base URLs")
base_urls = {}
for version in sorted(specs.keys()):
    default_url = BASE_URLS.get(version, f"http://localhost:8080/api/{version}")
    base_urls[version] = st.sidebar.text_input(f"{version} URL", value=default_url, key=f"url_{version}")

# Version selection for comparison
st.sidebar.subheader("Compare Versions")
version_list = sorted(specs.keys())

if len(version_list) >= 2:
    col1, col2 = st.sidebar.columns(2)
    with col1:
        version_a = st.selectbox("From", version_list, index=0, key="va")
    with col2:
        version_b = st.selectbox("To", version_list, index=min(1, len(version_list) - 1), key="vb")
else:
    version_a = version_list[0] if version_list else None
    version_b = version_list[1] if len(version_list) > 1 else version_list[0] if version_list else None


# ═══════════════════════════════════════════
# MAIN CONTENT
# ═══════════════════════════════════════════

st.title("🔀 API Version Diff Tool")
st.caption("Compare Swagger specs, validate samples, test live endpoints, and reconcile spec vs reality")

if not specs:
    st.warning("Please load at least one API spec to get started.")
    st.stop()

if len(specs) < 2:
    st.warning("Please load at least two API specs to compare.")
    st.stop()

# ─── TABS ───
tab_overview, tab_spec_diff, tab_schema_detail, tab_samples, tab_live, tab_reconcile, tab_excel_curl, tab_merge, tab_smart = st.tabs([
    "📊 Overview",
    "📋 Spec Diff",
    "🔍 Schema Detail",
    "✅ Sample Validation",
    "🌐 Live Response Diff",
    "🔗 Reconciliation",
    "📂 Excel CURL Runner",
    "🔀 Merge Coverage",
    "🧠 Smart Analysis",
])


# ═══ TAB 1: Overview ═══
with tab_overview:
    st.header(f"Overview: {version_a} → {version_b}")

    spec_a = specs[version_a]
    spec_b = specs[version_b]

    ep_diff = diff_endpoints(spec_a, spec_b, version_a, version_b)
    summary = ep_diff["summary"]

    # Summary metrics
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric(f"Endpoints in {version_a}", summary["total_in_a"])
    c2.metric(f"Endpoints in {version_b}", summary["total_in_b"])
    c3.metric("Added", summary["added_count"], delta=summary["added_count"] if summary["added_count"] else None)
    c4.metric("Removed", summary["removed_count"],
              delta=-summary["removed_count"] if summary["removed_count"] else None, delta_color="inverse")
    c5.metric("Common", summary["common_count"])

    # Breaking changes count
    endpoints_a = extract_endpoints(spec_a)
    endpoints_b = extract_endpoints(spec_b)
    breaking_count = 0
    for ep_key in ep_diff["common"]:
        ed = full_endpoint_diff(endpoints_a[ep_key], endpoints_b[ep_key])
        if ed["has_breaking_changes"]:
            breaking_count += 1

    if breaking_count > 0:
        st.error(f"⚠️ {breaking_count} endpoint(s) with BREAKING changes detected")
    else:
        st.success("✅ No breaking changes in common endpoints")

    # All versions quick summary
    st.subheader("All Versions at a Glance")
    version_data = []
    for v in sorted(specs.keys()):
        ep = extract_endpoints(specs[v])
        schemas = extract_schemas(specs[v])
        info = specs[v].get("info", {})
        version_data.append({
            "Version": v,
            "Title": info.get("title", ""),
            "API Version": info.get("version", ""),
            "Endpoints": len(ep),
            "Schemas": len(schemas),
            "Description": info.get("description", "")[:100],
        })
    st.dataframe(pd.DataFrame(version_data), use_container_width=True, hide_index=True)

    # Multi-version endpoint matrix
    st.subheader("Endpoint Availability Matrix")
    all_endpoints = set()
    for v in specs:
        all_endpoints.update(extract_endpoints(specs[v]).keys())

    matrix_data = []
    for ep in sorted(all_endpoints):
        row = {"Endpoint": ep}
        for v in sorted(specs.keys()):
            row[v] = "✅" if ep in extract_endpoints(specs[v]) else "❌"
        matrix_data.append(row)
    st.dataframe(pd.DataFrame(matrix_data), use_container_width=True, hide_index=True)


# ═══ TAB 2: Spec Diff ═══
with tab_spec_diff:
    st.header(f"Spec Diff: {version_a} → {version_b}")

    spec_a = specs[version_a]
    spec_b = specs[version_b]
    ep_diff = diff_endpoints(spec_a, spec_b, version_a, version_b)

    # Added endpoints
    if ep_diff["added"]:
        st.subheader(f"🟢 Added Endpoints ({len(ep_diff['added'])})")
        for ep_key, ep_info in ep_diff["added"].items():
            with st.expander(f"**{ep_key}** — {ep_info.get('summary', '')}"):
                st.write(f"**Operation ID:** {ep_info.get('operation_id', 'N/A')}")
                st.write(f"**Parameters:** {len(ep_info.get('parameters', []))}")
                has_body = ep_info.get('request_body') is not None
                st.write(f"**Has Request Body:** {has_body}")
                st.write(f"**Response Codes:** {', '.join(ep_info.get('responses', {}).keys())}")

    # Removed endpoints
    if ep_diff["removed"]:
        st.subheader(f"🔴 Removed Endpoints ({len(ep_diff['removed'])})")
        for ep_key, ep_info in ep_diff["removed"].items():
            with st.expander(f"**{ep_key}** — {ep_info.get('summary', '')}"):
                st.write(f"**Operation ID:** {ep_info.get('operation_id', 'N/A')}")
                st.warning("This endpoint was removed in the newer version")

    # Method changes
    if ep_diff["method_changes"]:
        st.subheader("🟡 Method Changes")
        for mc in ep_diff["method_changes"]:
            st.write(f"**{mc['path']}**")
            if mc["methods_added"]:
                st.write(f"  Added: {', '.join(mc['methods_added'])}")
            if mc["methods_removed"]:
                st.write(f"  Removed: {', '.join(mc['methods_removed'])}")

    # Common endpoints with changes
    st.subheader(f"Common Endpoints ({len(ep_diff['common'])})")
    endpoints_a = extract_endpoints(spec_a)
    endpoints_b = extract_endpoints(spec_b)

    for ep_key in ep_diff["common"]:
        ed = full_endpoint_diff(endpoints_a[ep_key], endpoints_b[ep_key])
        if ed["total_changes"] == 0:
            continue

        status_icon = "🔴" if ed["has_breaking_changes"] else "🟡"
        with st.expander(f"{status_icon} **{ep_key}** — {ed['total_changes']} change(s)"):
            if ed["parameter_changes"]:
                st.write("**Parameter Changes:**")
                param_rows = []
                for pc in ed["parameter_changes"]:
                    param_rows.append({
                        "Parameter": pc.get("parameter", pc.get("field", "")),
                        "Change": pc["change"],
                        "Severity": pc["severity"],
                        "Detail": pc["detail"],
                    })
                st.dataframe(pd.DataFrame(param_rows), use_container_width=True, hide_index=True)

            if ed["request_schema_changes"]:
                st.write("**Request Schema Changes:**")
                req_rows = []
                for rc in ed["request_schema_changes"]:
                    req_rows.append({
                        "Field": rc["field"],
                        "Change": rc["change"],
                        "Severity": rc["severity"],
                        "Detail": rc["detail"],
                    })
                st.dataframe(pd.DataFrame(req_rows), use_container_width=True, hide_index=True)

            if ed["response_schema_changes"]:
                st.write("**Response Schema Changes:**")
                resp_rows = []
                for rc in ed["response_schema_changes"]:
                    resp_rows.append({
                        "Field": rc["field"],
                        "Change": rc["change"],
                        "Severity": rc["severity"],
                        "Detail": rc["detail"],
                    })
                st.dataframe(pd.DataFrame(resp_rows), use_container_width=True, hide_index=True)

            if ed["response_code_changes"]:
                st.write("**Response Code Changes:**")
                for cc in ed["response_code_changes"]:
                    st.markdown(severity_html(cc["severity"], cc["detail"]), unsafe_allow_html=True)


# ═══ TAB 3: Schema Detail ═══
with tab_schema_detail:
    st.header("Schema Deep Dive")

    schema_version = st.selectbox("Select version", sorted(specs.keys()), key="schema_ver")
    spec = specs[schema_version]
    schemas = extract_schemas(spec)

    if not schemas:
        st.info("No schemas found in this spec.")
    else:
        schema_name = st.selectbox("Select schema", sorted(schemas.keys()))
        schema = schemas[schema_name]

        col_tree, col_flat = st.columns(2)

        with col_tree:
            st.subheader("Schema Tree")
            st.json(schema)

        with col_flat:
            st.subheader("Flattened Fields")
            flat = flatten_schema(schema)
            if flat:
                flat_rows = []
                for path, info in flat.items():
                    flat_rows.append({
                        "Field Path": path,
                        "Type": info.get("type", ""),
                        "Required": "✅" if info.get("required") else "",
                        "Format": info.get("format", ""),
                        "Enum": str(info.get("enum", "")) if info.get("enum") else "",
                    })
                st.dataframe(pd.DataFrame(flat_rows), use_container_width=True, hide_index=True)

        # Side-by-side schema comparison
        st.divider()
        st.subheader("Compare Schema Across Versions")
        if schema_name in extract_schemas(specs.get(version_a, {})) and schema_name in extract_schemas(specs.get(version_b, {})):
            schema_a_data = extract_schemas(specs[version_a])[schema_name]
            schema_b_data = extract_schemas(specs[version_b])[schema_name]

            from differ.schema_diff import diff_flat_schemas
            schema_changes = diff_flat_schemas(schema_a_data, schema_b_data)

            if schema_changes:
                change_rows = []
                for sc in schema_changes:
                    change_rows.append({
                        "Field": sc["field"],
                        "Change": sc["change"],
                        "Severity": sc["severity"],
                        "Detail": sc["detail"],
                    })
                st.dataframe(pd.DataFrame(change_rows), use_container_width=True, hide_index=True)
            else:
                st.success(f"No changes to schema '{schema_name}' between {version_a} and {version_b}")
        else:
            st.info(f"Schema '{schema_name}' not present in both {version_a} and {version_b}")


# ═══ TAB 4: Sample Validation ═══
with tab_samples:
    st.header("Sample Input Validation")

    if not samples:
        st.info("No sample inputs loaded. Add samples to samples/samples.json")
        st.code(json.dumps({
            "GET /users": {"query_params": {"page": 1}},
            "POST /users": {"body": {"name": "Test", "email": "test@example.com"}},
        }, indent=2), language="json")
    else:
        # Edit samples inline
        with st.expander("📝 Edit Samples"):
            edited_samples = st.text_area(
                "Samples JSON",
                value=json.dumps(samples, indent=2),
                height=300,
                key="samples_editor"
            )
            if st.button("Update Samples"):
                try:
                    samples = json.loads(edited_samples)
                    st.success("Samples updated!")
                except json.JSONDecodeError as e:
                    st.error(f"Invalid JSON: {e}")

        validation_results = validate_samples_across_versions(samples, specs)

        # Validation matrix
        st.subheader("Validation Matrix")
        matrix_rows = []
        for ep_key, versions in validation_results.items():
            row = {"Endpoint": ep_key}
            for v in sorted(versions.keys()):
                result = versions[v]
                status = result["status"]
                icon = {"OK": "✅", "WARNING": "⚠️", "INVALID": "❌", "NOT_FOUND": "🚫"}.get(status, "❓")
                row[v] = f"{icon} {status}"
            matrix_rows.append(row)
        st.dataframe(pd.DataFrame(matrix_rows), use_container_width=True, hide_index=True)

        # Detailed validation results
        st.subheader("Detailed Results")
        for ep_key, versions in validation_results.items():
            with st.expander(f"**{ep_key}**"):
                for v in sorted(versions.keys()):
                    result = versions[v]
                    status = result["status"]
                    st.write(f"**{v}**: {result['message']}")

                    if result.get("body_validation") and not result["body_validation"]["valid"]:
                        for err in result["body_validation"]["errors"]:
                            st.markdown(
                                severity_html("BREAKING", f"`{err['path']}`: {err['message']}"),
                                unsafe_allow_html=True
                            )

                    for pw in result.get("param_warnings", []):
                        st.markdown(
                            severity_html("MODIFIED", f"Param `{pw['param']}`: {pw['message']}"),
                            unsafe_allow_html=True
                        )


# ═══ TAB 5: Live Response Diff ═══
with tab_live:
    st.header("Live Response Comparison")
    st.caption(f"Call endpoints on {version_a} and {version_b} with the same inputs and compare responses")

    # Endpoint selection
    common_endpoints = set(extract_endpoints(specs[version_a]).keys()) & set(extract_endpoints(specs[version_b]).keys())
    if not common_endpoints:
        st.warning(f"No common endpoints between {version_a} and {version_b}")
    else:
        selected_ep = st.selectbox(
            "Select endpoint to test",
            sorted(common_endpoints),
            key="live_ep"
        )

        # Show/edit sample for this endpoint
        sample = samples.get(selected_ep, {})
        st.write("**Sample Input:**")
        edited_sample = st.text_area(
            "Edit sample input (JSON)",
            value=json.dumps(sample, indent=2),
            height=150,
            key="live_sample"
        )
        try:
            sample = json.loads(edited_sample)
        except json.JSONDecodeError:
            st.error("Invalid JSON for sample input")
            sample = {}

        # Custom headers
        with st.expander("Custom Headers"):
            custom_headers_str = st.text_area(
                "Headers JSON",
                value=json.dumps({"Content-Type": "application/json"}, indent=2),
                height=100,
                key="custom_headers",
            )
            try:
                custom_headers = json.loads(custom_headers_str)
            except Exception:
                custom_headers = {}

        col_call, col_snap = st.columns(2)
        call_live = col_call.button("🚀 Call Live Endpoints", type="primary")
        use_snapshots = col_snap.button("📸 Use Saved Snapshots")

        if call_live:
            with st.spinner(f"Calling {selected_ep} on {version_a} and {version_b}..."):
                selected_specs = {version_a: specs[version_a], version_b: specs[version_b]}
                responses = call_across_versions(
                    endpoint_key=selected_ep,
                    sample=sample,
                    specs=selected_specs,
                    base_urls=base_urls,
                    headers=custom_headers,
                    timeout=REQUEST_TIMEOUT,
                )

                # Save snapshots
                for v, resp in responses.items():
                    save_snapshot(SNAPSHOTS_DIR, selected_ep, v, resp, sample)

                st.session_state["live_responses"] = responses
                st.session_state["live_endpoint"] = selected_ep

        if use_snapshots:
            snap_a = load_snapshot(SNAPSHOTS_DIR, selected_ep, version_a)
            snap_b = load_snapshot(SNAPSHOTS_DIR, selected_ep, version_b)
            if snap_a and snap_b:
                st.session_state["live_responses"] = {
                    version_a: snap_a["response"],
                    version_b: snap_b["response"],
                }
                st.session_state["live_endpoint"] = selected_ep
                st.info(f"Loaded snapshots from {snap_a['captured_at']} and {snap_b['captured_at']}")
            else:
                st.warning("No saved snapshots found for this endpoint. Call live endpoints first.")

        # Display results
        if "live_responses" in st.session_state and st.session_state.get("live_endpoint") == selected_ep:
            responses = st.session_state["live_responses"]
            resp_a = responses.get(version_a, {})
            resp_b = responses.get(version_b, {})

            # Response summary
            st.divider()
            rc1, rc2 = st.columns(2)
            with rc1:
                st.subheader(f"📤 {version_a}")
                if resp_a.get("error"):
                    st.error(resp_a["error"])
                else:
                    st.metric("Status", resp_a.get("status_code"))
                    st.metric("Time", f"{resp_a.get('response_time_ms', 0)}ms")
                    st.write(f"**URL:** `{resp_a.get('url', '')}`")
            with rc2:
                st.subheader(f"📤 {version_b}")
                if resp_b.get("error"):
                    st.error(resp_b["error"])
                else:
                    st.metric("Status", resp_b.get("status_code"))
                    st.metric("Time", f"{resp_b.get('response_time_ms', 0)}ms")
                    st.write(f"**URL:** `{resp_b.get('url', '')}`")

            # Side-by-side response bodies
            st.subheader("Response Bodies")
            body_col1, body_col2 = st.columns(2)
            with body_col1:
                st.write(f"**{version_a}:**")
                if resp_a.get("body"):
                    st.json(resp_a["body"])
                else:
                    st.code("(no body)" if not resp_a.get("error") else resp_a.get("error", ""))
            with body_col2:
                st.write(f"**{version_b}:**")
                if resp_b.get("body"):
                    st.json(resp_b["body"])
                else:
                    st.code("(no body)" if not resp_b.get("error") else resp_b.get("error", ""))

            # Deep diff
            if not resp_a.get("error") and not resp_b.get("error"):
                st.subheader("Response Diff Analysis")
                resp_diff = diff_responses(resp_a, resp_b, version_a, version_b)

                overall = resp_diff["overall_severity"]
                st.markdown(f"**Overall Severity:** {severity_badge(overall)}")

                if resp_diff["status_code_diff"]:
                    scd = resp_diff["status_code_diff"]
                    st.markdown(severity_html("BREAKING", scd["detail"]), unsafe_allow_html=True)

                if resp_diff["timing_diff"]:
                    td = resp_diff["timing_diff"]
                    direction = "slower" if td["difference_ms"] > 0 else "faster"
                    st.write(f"**Timing:** {version_b} is {abs(td['difference_ms'])}ms {direction} ({td['percentage_change']}%)")

                if resp_diff["body_diffs"]:
                    st.write("**Body Differences:**")
                    diff_rows = []
                    for bd in resp_diff["body_diffs"]:
                        diff_rows.append({
                            "Path": bd.get("path", ""),
                            "Change": bd.get("change", ""),
                            "Severity": bd.get("severity", ""),
                            version_a: str(bd.get(version_a, ""))[:100],
                            version_b: str(bd.get(version_b, ""))[:100],
                        })
                    st.dataframe(pd.DataFrame(diff_rows), use_container_width=True, hide_index=True)

                if resp_diff["structure_diffs"]:
                    st.write("**Structural Differences:**")
                    for sd in resp_diff["structure_diffs"]:
                        st.markdown(severity_html(sd["severity"], sd["detail"]), unsafe_allow_html=True)

                if resp_diff["header_diffs"]:
                    st.write("**Header Differences:**")
                    st.dataframe(pd.DataFrame(resp_diff["header_diffs"]), use_container_width=True, hide_index=True)

                # Store for reconciliation
                st.session_state["last_resp_diff"] = resp_diff


# ═══ TAB 6: Reconciliation ═══
with tab_reconcile:
    st.header("Spec vs Reality Reconciliation")
    st.caption("Cross-references what the spec says changed vs what actually changed in live responses")

    if "live_responses" not in st.session_state:
        st.info("👈 Go to the **Live Response Diff** tab first to call endpoints, then come back here.")
    else:
        ep_key = st.session_state.get("live_endpoint", "")
        st.write(f"**Endpoint:** `{ep_key}`")
        st.write(f"**Comparing:** {version_a} → {version_b}")

        # Get spec diff for this endpoint
        endpoints_a = extract_endpoints(specs[version_a])
        endpoints_b = extract_endpoints(specs[version_b])

        if ep_key in endpoints_a and ep_key in endpoints_b:
            spec_diff = full_endpoint_diff(endpoints_a[ep_key], endpoints_b[ep_key])
            resp_diff = st.session_state.get("last_resp_diff", {})

            if resp_diff:
                findings = reconcile(spec_diff, resp_diff, version_a, version_b)

                # Summary
                match_count = sum(1 for f in findings if f["status"] == "MATCH")
                mismatch_count = sum(1 for f in findings if f["status"] == "MISMATCH")
                outdated_count = sum(1 for f in findings if f["status"] == "SPEC_OUTDATED")
                unconfirmed_count = sum(1 for f in findings if f["status"] == "UNCONFIRMED")

                mc1, mc2, mc3, mc4 = st.columns(4)
                mc1.metric("✅ Confirmed", match_count)
                mc2.metric("❌ Mismatched", mismatch_count)
                mc3.metric("📝 Spec Outdated", outdated_count)
                mc4.metric("❓ Unconfirmed", unconfirmed_count)

                # Findings table
                st.subheader("Findings")
                finding_rows = []
                for f in findings:
                    status_icon = {
                        "MATCH": "✅",
                        "MISMATCH": "❌",
                        "SPEC_OUTDATED": "📝",
                        "UNCONFIRMED": "❓",
                        "OK": "✅",
                    }.get(f["status"], "❓")

                    finding_rows.append({
                        "Status": f"{status_icon} {f['status']}",
                        "Field": f["field"],
                        "Spec Says": f["spec_says"],
                        "Reality": f["reality"],
                        "Detail": f["detail"],
                    })
                st.dataframe(pd.DataFrame(finding_rows), use_container_width=True, hide_index=True)

                # Detailed findings
                for f in findings:
                    if f["status"] == "MISMATCH":
                        st.markdown(severity_html("BREAKING", f"**MISMATCH**: {f['detail']}"), unsafe_allow_html=True)
                    elif f["status"] == "SPEC_OUTDATED":
                        st.markdown(severity_html("MODIFIED", f"**SPEC OUTDATED**: {f['detail']}"), unsafe_allow_html=True)
            else:
                st.info("No response diff data available. Run a live comparison first.")
        else:
            st.warning(f"Endpoint {ep_key} not found in both versions.")


# ═══ TAB 7: Excel CURL Runner ═══
with tab_excel_curl:
    st.header("📂 Excel CURL Runner")
    st.caption(
        "Upload an Excel/CSV file with **API NAME**, **CURLs**, and **OWNER** columns. "
        "The OWNER cell must contain `token: curl ...` — that curl will be executed to fetch the real "
        "bearer token which replaces the placeholder in the CURLs column."
    )

    # ── Upload ──
    uploaded_excel = st.file_uploader(
        "Upload Excel or CSV file",
        type=["xlsx", "xls", "csv"],
        key="excel_upload",
        help="Required columns: API NAME, CURLs, OWNER",
    )

    if not uploaded_excel:
        st.info("👆 Upload your Excel/CSV file to get started.")
        with st.expander("📌 Expected Format"):
            st.markdown("""
| API NAME | CURLs | OWNER |
|----------|-------|-------|
| GET /accounts/v3/account/{accountId}/controls | `curl -X GET 'https://api.example.com/...' -H 'authorization: Bearer PLACEHOLDER'` | John Doe token: `curl -X POST 'https://auth.example.com/token' -d 'client_id=xxx&client_secret=yyy'` |

**Key rules:**
- `CURLs` column must contain `authorization: Bearer <anything>` — this placeholder gets replaced.
- `OWNER` column must contain `token:` followed by the curl that fetches the real token.
- The token curl's response should be JSON with `access_token`, `token`, or `id_token` field, or a plain JWT string.
            """)
    else:
        # ── Parse Excel ──
        df_raw = None
        try:
            df_raw = load_excel(uploaded_excel)
        except ValueError as e:
            st.error(f"❌ {e}")
        except Exception as e:
            st.error(f"❌ Failed to read file: {e}")

        if df_raw is not None:
            st.success(f"✅ Loaded **{len(df_raw)} row(s)** from file")

            # ── Preview table ──
            with st.expander("🔍 Preview raw data", expanded=False):
                preview_df = df_raw[["api_name", "owner_name"]].copy()
                preview_df["has_token_curl"] = df_raw["token_curl"].notna().map({True: "✅", False: "❌"})
                preview_df["curls_snippet"] = df_raw["curls"].str[:120] + "…"
                st.dataframe(preview_df.rename(columns={
                    "api_name": "API Name",
                    "owner_name": "Owner",
                    "has_token_curl": "Token Curl Found",
                    "curls_snippet": "CURLs (preview)",
                }), use_container_width=True, hide_index=True)

            # ── Per-owner token fetching ──
            st.subheader("Step 1 — Fetch Tokens")
            st.markdown(
                "Each unique owner's `token:` curl will be executed once. "
                "The resulting token is then injected into all API curls for that owner."
            )

            # Group by unique token curl
            unique_token_curls = df_raw[["owner_name", "token_curl"]].dropna(subset=["token_curl"]).drop_duplicates("token_curl")
            no_token_rows = df_raw[df_raw["token_curl"].isna()]

            if not no_token_rows.empty:
                st.warning(
                    f"⚠️ {len(no_token_rows)} row(s) have no `token:` curl in OWNER — "
                    "their bearer token will NOT be replaced: " +
                    ", ".join(no_token_rows["api_name"].tolist())
                )

            timeout_secs = st.slider("Request timeout (seconds)", min_value=5, max_value=120, value=30, step=5)

            col_fetch, col_run = st.columns([1, 1])
            do_fetch_tokens = col_fetch.button("🔑 Fetch All Tokens", type="primary")
            do_run_all     = col_run.button("🚀 Fetch Tokens + Run All APIs")

            # ── Session state init ──
            if "ec_tokens" not in st.session_state:
                st.session_state["ec_tokens"]       = {}
            if "ec_patched_rows" not in st.session_state:
                st.session_state["ec_patched_rows"] = []
            if "ec_results" not in st.session_state:
                st.session_state["ec_results"]      = []

            def _fetch_all_tokens(timeout):
                tokens = {}
                prog = st.progress(0, text="Fetching tokens…")
                for i, (_, row) in enumerate(unique_token_curls.iterrows()):
                    owner  = row["owner_name"]
                    tcurl  = row["token_curl"]
                    prog.progress((i + 1) / max(len(unique_token_curls), 1), text=f"Fetching token for {owner}…")
                    result = fetch_token_from_curl(tcurl, timeout=timeout)
                    tokens[owner] = result
                prog.empty()
                st.session_state["ec_tokens"] = tokens
                return tokens

            def _build_patched_rows(tokens):
                patched = []
                for _, row in df_raw.iterrows():
                    owner    = row["owner_name"]
                    curl_cmd = row["curls"]
                    token_result = tokens.get(owner, {})
                    real_token = token_result.get("token") if token_result else None
                    patched_curl = patch_bearer_token(curl_cmd, real_token) if real_token else curl_cmd
                    patched.append({
                        "api_name":      row["api_name"],
                        "owner":         owner,
                        "original_curl": curl_cmd,
                        "patched_curl":  patched_curl,
                        "token":         real_token,
                        "token_error":   token_result.get("error") if token_result else "No token curl found",
                        "token_patched": real_token is not None,
                    })
                st.session_state["ec_patched_rows"] = patched
                return patched

            def _run_all_apis(patched_rows, timeout):
                results = []
                prog = st.progress(0, text="Running API calls…")
                for i, row in enumerate(patched_rows):
                    prog.progress((i + 1) / max(len(patched_rows), 1), text=f"Calling {row['api_name']}…")
                    if not row["token_patched"]:
                        results.append({**row, "status_code": None, "elapsed_ms": None,
                                        "body": None, "run_error": row["token_error"]})
                        continue
                    resp = execute_api_curl(row["patched_curl"], timeout=timeout)
                    results.append({**row,
                                    "status_code": resp["status_code"],
                                    "elapsed_ms":  resp["elapsed_ms"],
                                    "body":        resp["body"],
                                    "run_error":   resp["error"]})
                prog.empty()
                st.session_state["ec_results"] = results
                return results

            # ── Execute on button press ──
            if do_fetch_tokens or do_run_all:
                with st.spinner("Fetching tokens…"):
                    tokens = _fetch_all_tokens(timeout_secs)
                patched = _build_patched_rows(tokens)
                if do_run_all:
                    with st.spinner("Running API calls…"):
                        _run_all_apis(patched, timeout_secs)

            # ── Token status ──
            if st.session_state["ec_tokens"]:
                st.subheader("Token Fetch Results")
                token_rows = []
                for owner, res in st.session_state["ec_tokens"].items():
                    token_rows.append({
                        "Owner":           owner,
                        "Status":          "✅ Got Token" if res["token"] else "❌ Failed",
                        "Token (preview)": (res["token"] or "")[:40] + "…" if res["token"] else "",
                        "Time (ms)":       round(res["elapsed_ms"], 0),
                        "Error":           res["error"] or "",
                    })
                st.dataframe(pd.DataFrame(token_rows), use_container_width=True, hide_index=True)

            # ── Patched CURLs preview ──
            if st.session_state["ec_patched_rows"]:
                st.subheader("Step 2 — Patched CURLs Preview")
                with st.expander("View all patched curls", expanded=False):
                    for row in st.session_state["ec_patched_rows"]:
                        icon = "✅" if row["token_patched"] else "❌"
                        st.markdown(f"**{icon} {row['api_name']}** (owner: {row['owner']})")
                        if not row["token_patched"]:
                            st.error(f"Token not available: {row['token_error']}")
                        st.code(row["patched_curl"], language="bash")
                        st.divider()

                if not do_run_all:
                    if st.button("🚀 Run All Patched API Calls"):
                        with st.spinner("Running API calls…"):
                            _run_all_apis(st.session_state["ec_patched_rows"], timeout_secs)

            # ── Results table ──
            if st.session_state["ec_results"]:
                st.subheader("Step 3 — API Call Results")
                summary_rows = []
                for r in st.session_state["ec_results"]:
                    sc = r.get("status_code")
                    ok = isinstance(sc, int) and 200 <= sc < 300
                    summary_rows.append({
                        "API Name":  r["api_name"],
                        "Owner":     r["owner"],
                        "Status":    f"{'✅' if ok else '❌'} {sc}" if sc else f"❌ {r.get('run_error', 'N/A')}",
                        "Time (ms)": r.get("elapsed_ms", ""),
                        "Token OK":  "✅" if r["token_patched"] else "❌",
                    })
                st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

                st.subheader("Detailed Results")
                for r in st.session_state["ec_results"]:
                    sc = r.get("status_code")
                    ok = isinstance(sc, int) and 200 <= sc < 300
                    with st.expander(f"{'✅' if ok else '❌'} **{r['api_name']}** — Status: `{sc}` — {r.get('elapsed_ms', 'N/A')} ms"):
                        if r.get("run_error"):
                            st.error(r["run_error"])
                        if r.get("body"):
                            st.json(r["body"]) if isinstance(r["body"], (dict, list)) else st.code(str(r["body"]))
                        else:
                            st.info("(empty response body)")
                        with st.expander("🔧 Patched CURL used", expanded=False):
                            st.code(r["patched_curl"], language="bash")

                # ── Export ──
                import io
                export_rows = []
                for r in st.session_state["ec_results"]:
                    export_rows.append({
                        "API Name":    r["api_name"],
                        "Owner":       r["owner"],
                        "Status Code": r.get("status_code", ""),
                        "Time (ms)":   r.get("elapsed_ms", ""),
                        "Token OK":    r["token_patched"],
                        "Error":       r.get("run_error", ""),
                        "Response":    str(r.get("body", ""))[:500],
                    })
                buf = io.BytesIO()
                pd.DataFrame(export_rows).to_excel(buf, index=False)
                buf.seek(0)
                st.download_button(
                    label="⬇️ Download Results as Excel",
                    data=buf,
                    file_name="curl_runner_results.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )


# ═══ TAB 8: Merge Coverage ═══
with tab_merge:
    st.header("🔀 Endpoint Merge Coverage")
    st.caption(
        "Analyse how well a **single v2 endpoint** covers multiple **v1 endpoints** "
        "that were consolidated/merged into it. "
        "Select the old endpoints and the new merged endpoint to see parameter, "
        "schema and status-code coverage."
    )

    # ── Version selectors ──
    _ver_list = sorted(specs.keys())
    mc_col1, mc_col2 = st.columns(2)
    with mc_col1:
        st.markdown("**1️⃣ Source version** (old — many endpoints)")
        src_ver = st.selectbox("Source version", _ver_list, key="mc_src", label_visibility="collapsed")
    with mc_col2:
        _tgt_options = [v for v in _ver_list if v != src_ver]
        st.markdown("**2️⃣ Target version** (new — merged endpoint)")
        tgt_ver = st.selectbox("Target version", _tgt_options if _tgt_options else _ver_list,
                               key="mc_tgt", label_visibility="collapsed")

    src_endpoints = extract_endpoints(specs[src_ver])
    tgt_endpoints = extract_endpoints(specs[tgt_ver])

    if not src_endpoints:
        st.warning(f"No endpoints found in spec **{src_ver}**. Check your spec file.")
    elif not tgt_endpoints:
        st.warning(f"No endpoints found in spec **{tgt_ver}**. Check your spec file.")
    else:
        mc_col3, mc_col4 = st.columns(2)
        with mc_col3:
            st.markdown(f"**3️⃣ Source endpoints to merge** ({src_ver})")
            selected_v1_keys = st.multiselect(
                "Source endpoints", options=sorted(src_endpoints.keys()),
                key="mc_v1_eps", label_visibility="collapsed",
                placeholder="Select one or more v1 endpoints…",
            )
        with mc_col4:
            st.markdown(f"**4️⃣ Target merged endpoint** ({tgt_ver})")
            _v2_options = [""] + sorted(tgt_endpoints.keys())
            selected_v2_key = st.selectbox(
                "Target endpoint", options=_v2_options,
                key="mc_v2_ep", label_visibility="collapsed",
            )

        run_coverage = st.button(
            "📊 Compute Coverage", type="primary",
            disabled=not (selected_v1_keys and selected_v2_key),
        )

        # ── Guidance messages ──
        if not selected_v1_keys:
            st.info("☝️ Select one or more source (v1) endpoints above.")
        elif not selected_v2_key:
            st.info("☝️ Select the merged target (v2) endpoint above.")
        else:
            # ── Compute on button click ──
            if run_coverage:
                try:
                    _v1_selected = {k: src_endpoints[k] for k in selected_v1_keys}
                    _result = compute_merge_coverage(_v1_selected, tgt_endpoints[selected_v2_key], selected_v2_key)
                    _per_ep = per_endpoint_coverage(_v1_selected, tgt_endpoints[selected_v2_key])
                    st.session_state["mc_last_result"]   = _result
                    st.session_state["mc_per_ep"]        = _per_ep
                    st.session_state["mc_sel_v1"]        = selected_v1_keys
                    st.session_state["mc_sel_v2"]        = selected_v2_key
                except Exception as _exc:
                    st.error(f"❌ Coverage computation failed: {_exc}")
                    st.session_state.pop("mc_last_result", None)

            # ── Show cached or fresh results ──
            _cached = st.session_state.get("mc_last_result")
            if _cached and st.session_state.get("mc_sel_v2") == selected_v2_key:
                result = _cached
                per_ep = st.session_state.get("mc_per_ep", [])

                overall = result["overall_pct"]
                grade   = coverage_color(overall)
                color   = "green" if overall >= 90 else ("orange" if overall >= 60 else "red")
                st.markdown(
                    f'<div style="border-radius:12px;padding:20px;background:#1e1e2e;margin:12px 0">'
                    f'<div style="font-size:2.2rem;font-weight:700;color:{color}">{grade} {overall}% Overall Coverage</div>'
                    f'<div style="color:#aaa;margin-top:4px">'
                    f'<b>{len(result["v1_ep_keys"])} source endpoint(s)</b> → <b>{result["v2_ep_key"]}</b>'
                    f'</div></div>',
                    unsafe_allow_html=True,
                )

                # ── 4 metric cards ──
                _sections = [
                    ("Parameters",     result["parameters"]),
                    ("Request Schema", result["request_schema"]),
                    ("Resp Schema",    result["response_schema"]),
                    ("Status Codes",   result["response_codes"]),
                ]
                _cols = st.columns(4)
                for (_col, (_lbl, _sec)) in zip(_cols, _sections):
                    _col.metric(
                        label=f"{coverage_color(_sec['pct'])} {_lbl}",
                        value=f"{_sec['pct']}%",
                        delta=f"{_sec['covered']}/{_sec['total']} covered",
                    )

                # ── Progress bars ──
                st.markdown("---")
                st.subheader("Coverage Breakdown")
                _pcols = st.columns(4)
                for (_pc, (_lbl, _sec)) in zip(_pcols, _sections):
                    with _pc:
                        st.caption(_lbl)
                        st.progress(int(_sec["pct"]), text=f"{_sec['pct']}%")

                # ── Per-endpoint breakdown ──
                if per_ep:
                    st.divider()
                    st.subheader("Per-Source-Endpoint Breakdown")
                    st.caption("How well does the v2 endpoint cover each individual v1 endpoint?")
                    st.dataframe(pd.DataFrame(per_ep).set_index("v1_endpoint"), use_container_width=True)
                    st.markdown("**Grade:** 🟢 ≥90%  🟡 60–89%  🔴 <60%")

                # ── Detail gap analysis ──
                st.divider()
                st.subheader("Detailed Gap Analysis")
                _dt_params, _dt_req, _dt_resp, _dt_codes = st.tabs([
                    "🔧 Parameters", "📤 Request Schema", "📥 Response Schema", "🔢 Status Codes"
                ])

                def _show_gap_table(tab, rows, key_col, extra_cols=None):
                    with tab:
                        if not rows:
                            st.info("No data found for selected endpoints.")
                            return
                        _missing = [r for r in rows if not r["in_v2"]]
                        _covered = [r for r in rows if r["in_v2"]]
                        _mismatch = [r for r in _covered
                                     if r.get("type_v1") and r.get("type_v2") and r["type_v1"] != r["type_v2"]]
                        if _missing:
                            st.error(f"❌ {len(_missing)} missing in v2")
                            _df_m = pd.DataFrame(_missing)
                            _cols_m = [c for c in [key_col] + (extra_cols or []) if c in _df_m.columns]
                            st.dataframe(_df_m[_cols_m] if _cols_m else _df_m,
                                         use_container_width=True, hide_index=True)
                        if _mismatch:
                            st.warning(f"⚠️ {len(_mismatch)} type-changed")
                            _df_mm = pd.DataFrame(_mismatch)
                            _mm_cols = [c for c in [key_col, "type_v1", "type_v2"] if c in _df_mm.columns]
                            st.dataframe(_df_mm[_mm_cols] if _mm_cols else _df_mm,
                                         use_container_width=True, hide_index=True)
                        with st.expander(f"✅ {len(_covered)} covered"):
                            if _covered:
                                _df_c = pd.DataFrame(_covered)
                                _cols_c = [c for c in [key_col] + (extra_cols or []) if c in _df_c.columns]
                                st.dataframe(_df_c[_cols_c] if _cols_c else _df_c,
                                             use_container_width=True, hide_index=True)
                            else:
                                st.info("No covered items.")

                _show_gap_table(_dt_params, result["parameters"]["rows"],    "parameter", ["in", "required_v1"])
                _show_gap_table(_dt_req,    result["request_schema"]["rows"], "field",     ["type_v1", "required_v1"])
                _show_gap_table(_dt_resp,   result["response_schema"]["rows"],"field",     ["type_v1"])

                with _dt_codes:
                    st.dataframe(pd.DataFrame([{
                        "Code": r["code"], "In v1": "✅" if r["in_v1"] else "",
                        "In v2": "✅" if r["in_v2"] else "", "Status": r["status"],
                    } for r in result["response_codes"]["rows"]]),
                        use_container_width=True, hide_index=True)

                # ── Export ──
                st.divider()
                import io as _io
                _buf = _io.BytesIO()
                with pd.ExcelWriter(_buf, engine="openpyxl") as _w:
                    for _sname, _srows in [
                        ("Parameters",     result["parameters"]["rows"]),
                        ("Request Schema", result["request_schema"]["rows"]),
                        ("Response Schema",result["response_schema"]["rows"]),
                        ("Status Codes",   result["response_codes"]["rows"]),
                    ]:
                        if _srows:
                            pd.DataFrame(_srows).to_excel(_w, sheet_name=_sname[:31], index=False)
                    if per_ep:
                        pd.DataFrame(per_ep).to_excel(_w, sheet_name="Per Endpoint", index=False)
                _buf.seek(0)
                st.download_button(
                    "⬇️ Download Coverage Report (Excel)", data=_buf,
                    file_name="merge_coverage_report.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            else:
                st.info("☝️ Click **Compute Coverage** to run the analysis.")


# ═══ TAB 9: Smart Analysis ═══
with tab_smart:
    st.header("🧠 Smart Endpoint Analysis")
    st.caption(
        "Automatically detect which endpoints were **merged across versions** "
        "and which endpoints within the **same version** are redundant — "
        "without requiring manual selection."
    )

    smart_tab_merge, smart_tab_redundancy = st.tabs([
        "🔀 Cross-Version: Merge Detection",
        "♻️ Same-Version: Redundancy Detection",
    ])

    # ── Section 1: Cross-Version Merge Detection ──────────────────────────────
    with smart_tab_merge:
        st.subheader("Cross-Version Merge Detection")
        st.markdown(
            "Select two versions. The tool will **automatically score every (v1→v2) endpoint pair** "
            "and group v1 endpoints that were likely merged into the same v2 endpoint."
        )

        _sv_col1, _sv_col2 = st.columns(2)
        with _sv_col1:
            _sm_src = st.selectbox("Source version (old)", sorted(specs.keys()), key="sm_src")
        with _sv_col2:
            _sm_tgt_opts = [v for v in sorted(specs.keys()) if v != _sm_src]
            _sm_tgt = st.selectbox(
                "Target version (new)",
                _sm_tgt_opts if _sm_tgt_opts else sorted(specs.keys()),
                key="sm_tgt",
            )

        _sm_threshold = st.slider(
            "Minimum similarity threshold (%)",
            min_value=10, max_value=90, value=40, step=5,
            key="sm_threshold",
            help="Lower = more candidates shown (more noise). Higher = only strong matches.",
        )

        _sm_run = st.button("🔍 Detect Merge Groups", type="primary", key="sm_run")

        if _sm_run:
            _sm_src_eps = extract_endpoints(specs[_sm_src])
            _sm_tgt_eps = extract_endpoints(specs[_sm_tgt])
            with st.spinner("Scoring endpoint pairs…"):
                _sm_groups = find_merge_candidates(_sm_src_eps, _sm_tgt_eps, threshold=_sm_threshold)
            st.session_state["sm_groups"]  = _sm_groups
            st.session_state["sm_src_eps"] = _sm_src_eps
            st.session_state["sm_tgt_eps"] = _sm_tgt_eps

        _sm_groups = st.session_state.get("sm_groups")
        if _sm_groups is not None:
            st.markdown("---")
            if not _sm_groups:
                st.info(f"No merge groups found above {_sm_threshold}% similarity. Try lowering the threshold.")
            else:
                # Summary table
                _summary_rows = [{
                    "v2 Endpoint":        g["v2_key"],
                    "# v1 Candidates":    len(g["candidates"]),
                    "Avg Confidence":     f"{g['confidence']}%",
                    "Top v1 Match":       g["candidates"][0]["v1_key"],
                    "Top Score":          f"{g['candidates'][0]['score']}%",
                    "Verdict":            g["verdict"],
                } for g in _sm_groups]
                st.success(f"Found **{len(_sm_groups)} merge group(s)**")
                st.dataframe(pd.DataFrame(_summary_rows), use_container_width=True, hide_index=True)

                st.markdown("### Detailed Breakdown")
                for g in _sm_groups:
                    _n = len(g["candidates"])
                    _label = (
                        f"🔀 **{g['v2_key']}** ← {_n} v1 endpoint(s) "
                        f"(confidence {g['confidence']}%)"
                    )
                    with st.expander(_label):
                        st.markdown(f"**Verdict:** {g['verdict']}")
                        for c in g["candidates"]:
                            _bd = c["breakdown"]
                            st.markdown(
                                f"**{c['v1_key']}** — Overall: `{c['score']}%`  \n"
                                f"Resp Schema ★: `{_bd['resp_schema']}%` | "
                                f"Method ★: `{_bd['method']}%` | "
                                f"Req Schema: `{_bd['req_schema']}%` | "
                                f"Path: `{_bd['path']}%` | "
                                f"Params: `{_bd['params']}%`"
                            )
                            st.progress(int(c["score"]))
                            st.divider()

                        # Quick-fill button hint
                        _v1_keys = [c["v1_key"] for c in g["candidates"]]
                        st.info(
                            f"💡 Go to **🔀 Merge Coverage** tab → select source version **{_sm_src}**, "
                            f"target **{_sm_tgt}**, pick these v1 endpoints and **{g['v2_key']}** "
                            "to run a full coverage analysis."
                        )

    # ── Section 2: Same-Version Redundancy Detection ──────────────────────────
    with smart_tab_redundancy:
        st.subheader("Same-Version Redundancy Detection")
        st.markdown(
            "Select a single version. The tool will find all endpoint **pairs** "
            "with high structural similarity — strong candidates for consolidation."
        )

        _sr_ver = st.selectbox("Version to analyse", sorted(specs.keys()), key="sr_ver")
        _sr_threshold = st.slider(
            "Minimum similarity threshold (%)",
            min_value=20, max_value=95, value=60, step=5,
            key="sr_threshold",
            help="Lower = more pairs (more noise). 60%+ is a good starting point.",
        )

        _sr_run = st.button("🔍 Find Redundant Endpoints", type="primary", key="sr_run")

        if _sr_run:
            _sr_eps = extract_endpoints(specs[_sr_ver])
            _n_eps = len(_sr_eps)
            with st.spinner(f"Scoring {_n_eps * (_n_eps - 1) // 2} endpoint pairs…"):
                _sr_pairs = find_redundant_endpoints(_sr_eps, threshold=_sr_threshold)
            st.session_state["sr_pairs"] = _sr_pairs

        _sr_pairs = st.session_state.get("sr_pairs")
        if _sr_pairs is not None:
            st.markdown("---")
            if not _sr_pairs:
                st.info(f"No redundant pairs found above {_sr_threshold}%. All endpoints appear distinct.")
            else:
                # Summary
                st.warning(f"Found **{len(_sr_pairs)} similar pair(s)** above {_sr_threshold}% threshold")
                _sr_summary = [{
                    "Endpoint 1":  p["ep1_key"],
                    "Endpoint 2":  p["ep2_key"],
                    "Similarity":  f"{p['score']}%",
                    "Verdict":     p["verdict"],
                } for p in _sr_pairs]
                st.dataframe(pd.DataFrame(_sr_summary), use_container_width=True, hide_index=True)

                st.markdown("### Detailed Breakdown")
                for p in _sr_pairs:
                    _bd = p["breakdown"]
                    with st.expander(f"{p['verdict']}  — `{p['ep1_key']}` vs `{p['ep2_key']}`"):
                        _c1, _c2, _c3, _c4, _c5 = st.columns(5)
                        _c1.metric("Resp Schema ★", f"{_bd['resp_schema']}%")
                        _c2.metric("Method ★", f"{_bd['method']}%")
                        _c3.metric("Req Schema", f"{_bd['req_schema']}%")
                        _c4.metric("Path", f"{_bd['path']}%")
                        _c5.metric("Params", f"{_bd['params']}%")
                        st.progress(int(p["score"]), text=f"Overall {p['score']}%")

                        # Side-by-side signal bars
                        st.markdown("**Signal breakdown:**")
                        for sig, val in _bd.items():
                            st.caption(sig.replace("_", " ").title())
                            st.progress(int(val))


# ─── Footer ───
st.divider()
st.caption("Swagger API Diff Tool | Compare API versions, validate samples, test live endpoints")

