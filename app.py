import io
import streamlit as st
import pandas as pd
from pipeline import run_pipeline


@st.cache_data(show_spinner="Cleaning data, reading notes, scoring leads...")
def process_file(file_bytes: bytes):
    """Cached so that clicking around the app (picking a different lead,
    changing a filter) never re-reads the uploaded file. Streamlit reruns
    the whole script on every interaction, and re-reading the SAME
    uploaded-file object a second time returns empty content because its
    internal read pointer is already at the end - this was the cause of
    the KeyError. Using raw bytes + a fresh BytesIO each time avoids that,
    and caching means the CSV is only actually parsed once per file."""
    return run_pipeline(io.BytesIO(file_bytes))

st.set_page_config(page_title="Lead Triage", layout="wide")

st.title("Lead Triage System")
st.caption("Upload a lead export to get every lead cleaned, scored, and ranked.")

uploaded = st.file_uploader("Upload lead export (.csv)", type=["csv"])

if uploaded:
    ranked, removed, summary = process_file(uploaded.getvalue())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Contact Now", summary.get("Contact Now", 0))
    c2.metric("Nurture", summary.get("Nurture", 0))
    c3.metric("Disqualify", summary.get("Disqualify", 0))
    c4.metric("Removed pre-scoring (junk/dupes)", len(removed))

    st.divider()

    tab_table, tab_inspect, tab_removed, tab_logic = st.tabs(
        ["Ranked table", "Inspect a single lead", "What got removed", "How scoring works"]
    )

    # ---------------- TAB 1: overview table ----------------
    with tab_table:
        bucket_filter = st.multiselect(
            "Filter by bucket",
            options=["Contact Now", "Nurture", "Disqualify"],
            default=["Contact Now", "Nurture", "Disqualify"],
        )
        summary_cols = ["lead_id", "company", "score", "bucket", "reason"]
        view = ranked[ranked["bucket"].isin(bucket_filter)][summary_cols]
        st.dataframe(view, use_container_width=True, height=450, hide_index=True)

        st.download_button(
            "Download full ranked leads (CSV, all columns)",
            ranked.to_csv(index=False).encode("utf-8"),
            file_name="ranked_leads.csv",
            mime="text/csv",
        )

    # ---------------- TAB 2: single-lead inspector ----------------
    with tab_inspect:
        options = ranked["lead_id"] + " — " + ranked["company"].fillna("") + " (" + ranked["bucket"] + ")"
        choice = st.selectbox("Choose a lead", options)
        chosen_id = choice.split(" — ")[0]
        lead = ranked[ranked["lead_id"] == chosen_id].iloc[0]

        left, right = st.columns([1, 1])
        with left:
            st.subheader(f"{lead['company']}")
            st.markdown(f"**Bucket:** {lead['bucket']}  |  **Score:** {lead['score']}/100")
            st.markdown(f"**Contact:** {lead['name']} — {lead['email']}")
            st.markdown(f"**Employees (parsed):** {lead['employees_n']}  |  "
                        f"**Monthly budget (parsed):** {lead['monthly_budget_usd']}")
            st.markdown("**Original note:**")
            st.info(lead["notes"] if pd.notna(lead["notes"]) and lead["notes"] else "(no note provided)")

        with right:
            st.subheader("Score breakdown")
            st.markdown(f"- Budget: **{lead['budget_pts']} / 35**")
            st.markdown(f"- Urgency: **{lead['urgency_pts']} / 25**")
            st.markdown(f"- Decision authority: **{lead['authority_pts']} / 15**")
            st.markdown(f"- ICP fit: **{lead['fit_pts']} / 25**")
            st.markdown(f"### Total: {lead['score']} / 100 → **{lead['bucket']}**")
            st.markdown(f"*Plain-language reason: {lead['reason']}*")

    # ---------------- TAB 3: removed rows ----------------
    with tab_removed:
        st.dataframe(removed, use_container_width=True, hide_index=True)
        st.download_button(
            "Download removed/audit rows (CSV)",
            removed.to_csv(index=False).encode("utf-8"),
            file_name="removed_rows_audit.csv",
            mime="text/csv",
        )

    # ---------------- TAB 4: logic explainer ----------------
    with tab_logic:
        st.markdown(
            """
            **Score = Budget (0–35) + Urgency (0–25) + Decision authority (0–15) + Fit (0–25)**

            Non-buyers (job seekers, students, journalists, investors, competitors, spam)
            are disqualified automatically before scoring.

            | Score | Bucket |
            |---|---|
            | 65–100 | Contact Now |
            | 35–64 | Nurture |
            | 0–34 | Disqualify |
            """
        )
else:
    st.info("Upload a CSV to run the triage system.")
