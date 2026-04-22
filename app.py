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
tab_overview, tab_spec_diff, tab_schema_detail, tab_samples, tab_live, tab_reconcile, tab_excel_curl, tab_merge = st.tabs([
    "📊 Overview",
    "📋 Spec Diff",
    "🔍 Schema Detail",
    "✅ Sample Validation",
    "🌐 Live Response Diff",
    "🔗 Reconciliation",
    "📂 Excel CURL Runner",
    "🔀 Merge Coverage",
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
        st.stop()

    # ── Parse Excel ──
    try:
        df_raw = load_excel(uploaded_excel)
    except ValueError as e:
        st.error(f"❌ {e}")
        st.stop()
    except Exception as e:
        st.error(f"❌ Failed to read file: {e}")
        st.stop()

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
        st.session_state["ec_tokens"]       = {}   # owner_name → token_result dict
    if "ec_patched_rows" not in st.session_state:
        st.session_state["ec_patched_rows"] = []   # list of dicts
    if "ec_results" not in st.session_state:
        st.session_state["ec_results"]      = []   # list of dicts

    def _fetch_all_tokens(timeout):
        """Fetch tokens for all unique owners and store in session state."""
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
        """Patch bearer tokens in CURLs and build row list."""
        patched = []
        for _, row in df_raw.iterrows():
            owner   = row["owner_name"]
            curl_cmd = row["curls"]
            token_result = tokens.get(owner, {})
            real_token = token_result.get("token") if token_result else None

            patched_curl = patch_bearer_token(curl_cmd, real_token) if real_token else curl_cmd
            patched.append({
                "api_name":    row["api_name"],
                "owner":       owner,
                "original_curl": curl_cmd,
                "patched_curl": patched_curl,
                "token":       real_token,
                "token_error": token_result.get("error") if token_result else "No token curl found",
                "token_patched": real_token is not None,
            })
        st.session_state["ec_patched_rows"] = patched
        return patched

    def _run_all_apis(patched_rows, timeout):
        """Execute all patched API curls."""
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
                "Owner":      owner,
                "Status":     "✅ Got Token" if res["token"] else "❌ Failed",
                "Token (preview)": (res["token"] or "")[:40] + "…" if res["token"] else "",
                "Time (ms)": round(res["elapsed_ms"], 0),
                "Error":     res["error"] or "",
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

        # Run button for already-fetched
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
                "API Name":   r["api_name"],
                "Owner":      r["owner"],
                "Status":     f"{'✅' if ok else '❌'} {sc}" if sc else f"❌ {r.get('run_error', 'N/A')}",
                "Time (ms)": r.get("elapsed_ms", ""),
                "Token OK":   "✅" if r["token_patched"] else "❌",
            })
        st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

        # Detailed per-row drill-down
        st.subheader("Detailed Results")
        for r in st.session_state["ec_results"]:
            sc = r.get("status_code")
            ok = isinstance(sc, int) and 200 <= sc < 300
            icon = "✅" if ok else "❌"
            label = f"{icon} **{r['api_name']}** — Status: `{sc}` — {r.get('elapsed_ms', 'N/A')} ms"
            with st.expander(label):
                if r.get("run_error"):
                    st.error(r["run_error"])
                if r.get("body"):
                    if isinstance(r["body"], (dict, list)):
                        st.json(r["body"])
                    else:
                        st.code(str(r["body"]))
                else:
                    st.info("(empty response body)")
                with st.expander("🔧 Patched CURL used", expanded=False):
                    st.code(r["patched_curl"], language="bash")

        # ── Export results ──
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
        export_df = pd.DataFrame(export_rows)
        buf = io.BytesIO()
        export_df.to_excel(buf, index=False)
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

    if len(specs) < 2:
        st.warning("Load at least two specs (e.g. v1 and v2) to use this tab.")
        st.stop()

    st.markdown("### 1️⃣  Choose source version (the 'old' version with many endpoints)")
    src_ver = st.selectbox("Source version", sorted(specs.keys()), key="mc_src")

    st.markdown("### 2️⃣  Choose target version (the 'new' version with the merged endpoint)")
    tgt_ver = st.selectbox(
        "Target version",
        [v for v in sorted(specs.keys()) if v != src_ver],
        key="mc_tgt",
    )

    src_endpoints = extract_endpoints(specs[src_ver])
    tgt_endpoints = extract_endpoints(specs[tgt_ver])

    st.markdown("### 3️⃣  Select the v1 endpoints that were merged")
    st.caption("Hold Ctrl/Cmd to select multiple.")
    selected_v1_keys = st.multiselect(
        f"Source endpoints  ({src_ver})",
        options=sorted(src_endpoints.keys()),
        key="mc_v1_eps",
    )

    st.markdown("### 4️⃣  Select the single merged v2 endpoint")
    selected_v2_key = st.selectbox(
        f"Target endpoint  ({tgt_ver})",
        options=[""] + sorted(tgt_endpoints.keys()),
        key="mc_v2_ep",
    )

    run_coverage = st.button("📊 Compute Coverage", type="primary", disabled=not (selected_v1_keys and selected_v2_key))

    if not selected_v1_keys:
        st.info("Select at least one source (v1) endpoint above.")
    elif not selected_v2_key:
        st.info("Select the target (v2) merged endpoint above.")
    elif run_coverage or st.session_state.get("mc_last_result"):

        if run_coverage:
            v1_eps_selected = {k: src_endpoints[k] for k in selected_v1_keys}
            result = compute_merge_coverage(v1_eps_selected, tgt_endpoints[selected_v2_key], selected_v2_key)
            per_ep = per_endpoint_coverage(v1_eps_selected, tgt_endpoints[selected_v2_key])
            st.session_state["mc_last_result"] = result
            st.session_state["mc_per_ep"]     = per_ep
            st.session_state["mc_v1_keys"]    = selected_v1_keys
            st.session_state["mc_v2_key"]     = selected_v2_key
        else:
            result = st.session_state["mc_last_result"]
            per_ep = st.session_state["mc_per_ep"]

        # ── Summary banner ──
        overall = result["overall_pct"]
        grade   = coverage_color(overall)
        color   = "green" if overall >= 90 else ("orange" if overall >= 60 else "red")
        st.markdown(
            f"""
            <div style="border-radius:12px; padding:20px; background:#1e1e2e; margin-bottom:16px;">
                <div style="font-size:2.5rem; font-weight:700; color:{color};">{grade} {overall}% Overall Coverage</div>
                <div style="color:#aaa; margin-top:4px;">
                    <b>{len(result['v1_ep_keys'])} source endpoint(s)</b> →
                    <b>{result['v2_ep_key']}</b>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # ── 4 metric cards ──
        mc1, mc2, mc3, mc4 = st.columns(4)
        sections = [
            (mc1, "Parameters",      result["parameters"]),
            (mc2, "Request Schema",  result["request_schema"]),
            (mc3, "Response Schema", result["response_schema"]),
            (mc4, "Status Codes",    result["response_codes"]),
        ]
        for col, label, sec in sections:
            pct   = sec["pct"]
            emoji = coverage_color(pct)
            col.metric(
                label=f"{emoji} {label}",
                value=f"{pct}%",
                delta=f"{sec['covered']}/{sec['total']} covered",
            )

        # ── Progress bars ──
        st.markdown("---")
        st.subheader("Coverage Breakdown")
        bar_cols = st.columns(4)
        for (col, label, sec) in sections:
            with col:
                st.caption(label)
                st.progress(int(sec["pct"]), text=f"{sec['pct']}%")

        # ── Per-endpoint individual breakdown ──
        st.divider()
        st.subheader("Per-Source-Endpoint Breakdown")
        st.caption(
            "How well does the merged v2 endpoint cover each individual v1 endpoint?"
        )
        if per_ep:
            per_ep_df = pd.DataFrame(per_ep).set_index("v1_endpoint")
            st.dataframe(per_ep_df, use_container_width=True)

            # Heat-map style: colour the Overall % column
            st.markdown("**Grade legend:** 🟢 ≥90% &nbsp; 🟡 60-89% &nbsp; 🔴 <60%")

        # ── Detail tables ──
        st.divider()
        st.subheader("Detailed Gap Analysis")

        detail_tab_params, detail_tab_req, detail_tab_resp, detail_tab_codes = st.tabs([
            "🔧 Parameters", "📤 Request Schema", "📥 Response Schema", "🔢 Status Codes"
        ])

        with detail_tab_params:
            rows = result["parameters"]["rows"]
            if not rows:
                st.info("No parameters found in selected v1 endpoints.")
            else:
                missing = [r for r in rows if not r["in_v2"]]
                covered = [r for r in rows if r["in_v2"]]
                if missing:
                    st.error(f"❌ {len(missing)} parameter(s) missing in v2:")
                    st.dataframe(
                        pd.DataFrame([{
                            "Parameter":   r["parameter"],
                            "In":          r["in"],
                            "Required v1": "✅" if r["required_v1"] else "",
                            "Source endpoints": ", ".join(r["sources"]),
                        } for r in missing]),
                        use_container_width=True, hide_index=True,
                    )
                if covered:
                    with st.expander(f"✅ {len(covered)} parameter(s) covered"):
                        st.dataframe(
                            pd.DataFrame([{
                                "Parameter":   r["parameter"],
                                "In":          r["in"],
                                "Required v1": "✅" if r["required_v1"] else "",
                                "Required v2": "✅" if r["v2_required"] else "",
                            } for r in covered]),
                            use_container_width=True, hide_index=True,
                        )

        with detail_tab_req:
            rows = result["request_schema"]["rows"]
            if not rows:
                st.info("No request body schema fields found in selected v1 endpoints.")
            else:
                missing = [r for r in rows if not r["in_v2"]]
                covered = [r for r in rows if r["in_v2"]]
                type_mismatch = [
                    r for r in covered
                    if r["type_v1"] and r["type_v2"] and r["type_v1"] != r["type_v2"]
                ]
                if missing:
                    st.error(f"❌ {len(missing)} request field(s) missing in v2:")
                    st.dataframe(
                        pd.DataFrame([{"Field": r["field"], "Type v1": r["type_v1"],
                                       "Required v1": "✅" if r["required_v1"] else ""}
                                      for r in missing]),
                        use_container_width=True, hide_index=True,
                    )
                if type_mismatch:
                    st.warning(f"⚠️ {len(type_mismatch)} field(s) covered but with TYPE CHANGE:")
                    st.dataframe(
                        pd.DataFrame([{"Field": r["field"],
                                       "Type v1": r["type_v1"], "Type v2": r["type_v2"]}
                                      for r in type_mismatch]),
                        use_container_width=True, hide_index=True,
                    )
                if covered:
                    with st.expander(f"✅ {len(covered)} field(s) covered"):
                        st.dataframe(
                            pd.DataFrame([{"Field": r["field"],
                                           "Type v1": r["type_v1"], "Type v2": r["type_v2"]}
                                          for r in covered]),
                            use_container_width=True, hide_index=True,
                        )

        with detail_tab_resp:
            rows = result["response_schema"]["rows"]
            if not rows:
                st.info("No response body schema fields found in selected v1 endpoints.")
            else:
                missing = [r for r in rows if not r["in_v2"]]
                covered = [r for r in rows if r["in_v2"]]
                type_mismatch = [
                    r for r in covered
                    if r["type_v1"] and r["type_v2"] and r["type_v1"] != r["type_v2"]
                ]
                if missing:
                    st.error(f"❌ {len(missing)} response field(s) missing in v2:")
                    st.dataframe(
                        pd.DataFrame([{"Field": r["field"], "Type v1": r["type_v1"]}
                                      for r in missing]),
                        use_container_width=True, hide_index=True,
                    )
                if type_mismatch:
                    st.warning(f"⚠️ {len(type_mismatch)} field(s) covered but TYPE CHANGED:")
                    st.dataframe(
                        pd.DataFrame([{"Field": r["field"],
                                       "Type v1": r["type_v1"], "Type v2": r["type_v2"]}
                                      for r in type_mismatch]),
                        use_container_width=True, hide_index=True,
                    )
                if covered:
                    with st.expander(f"✅ {len(covered)} field(s) covered"):
                        st.dataframe(
                            pd.DataFrame([{"Field": r["field"],
                                           "Type v1": r["type_v1"], "Type v2": r["type_v2"]}
                                          for r in covered]),
                            use_container_width=True, hide_index=True,
                        )

        with detail_tab_codes:
            rows = result["response_codes"]["rows"]
            st.dataframe(
                pd.DataFrame([{
                    "Status Code": r["code"],
                    "In v1": "✅" if r["in_v1"] else "",
                    "In v2": "✅" if r["in_v2"] else "",
                    "Status": r["status"],
                } for r in rows]),
                use_container_width=True, hide_index=True,
            )

        # ── Export ──
        st.divider()
        import io as _io
        export_sections = {
            "Parameters":      result["parameters"]["rows"],
            "Request Schema":  result["request_schema"]["rows"],
            "Response Schema": result["response_schema"]["rows"],
            "Status Codes":    result["response_codes"]["rows"],
        }
        buf2 = _io.BytesIO()
        with pd.ExcelWriter(buf2, engine="openpyxl") as writer:
            for sheet, rows in export_sections.items():
                if rows:
                    pd.DataFrame(rows).to_excel(writer, sheet_name=sheet[:31], index=False)
            if per_ep:
                pd.DataFrame(per_ep).to_excel(writer, sheet_name="Per Endpoint", index=False)
        buf2.seek(0)
        st.download_button(
            label="⬇️ Download Coverage Report (Excel)",
            data=buf2,
            file_name="merge_coverage_report.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


# ─── Footer ───
st.divider()
st.caption("Swagger API Diff Tool | Compare API versions, validate samples, test live endpoints")
