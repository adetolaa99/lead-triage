import streamlit as st
import pandas as pd
from pipeline import run_pipeline

st.set_page_config(page_title="Lead Triage", layout="wide")

st.title("Lead Triage System")
st.caption(
    "Upload any lead export with columns lead_id, created, name, email, "
    "company, employees, website, title, source, monthly_budget, notes. "
    "The system cleans, deduplicates, reads the notes, scores intent + fit, "
    "and ranks every lead."
)

uploaded = st.file_uploader("Upload lead export (.csv)", type=["csv"])

if uploaded:
    with st.spinner("Cleaning data, reading notes, scoring leads..."):
        ranked, removed, summary = run_pipeline(uploaded)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Contact Now", summary.get("Contact Now", 0))
    c2.metric("Nurture", summary.get("Nurture", 0))
    c3.metric("Disqualify", summary.get("Disqualify", 0))
    c4.metric("Removed pre-scoring\n(junk/dupes)", len(removed))

    st.divider()

    bucket_filter = st.multiselect(
        "Filter by bucket",
        options=["Contact Now", "Nurture", "Disqualify"],
        default=["Contact Now", "Nurture", "Disqualify"],
    )
    view = ranked[ranked["bucket"].isin(bucket_filter)]
    st.dataframe(view, use_container_width=True, height=500)

    col_a, col_b = st.columns(2)
    with col_a:
        st.download_button(
            "Download ranked leads (CSV)",
            ranked.to_csv(index=False).encode("utf-8"),
            file_name="ranked_leads.csv",
            mime="text/csv",
        )
    with col_b:
        st.download_button(
            "Download removed/audit rows (CSV)",
            removed.to_csv(index=False).encode("utf-8"),
            file_name="removed_rows_audit.csv",
            mime="text/csv",
        )

    with st.expander("What got removed before scoring, and why"):
        st.dataframe(removed, use_container_width=True)

    with st.expander("Scoring logic (read-only reference)"):
        st.markdown(
            """
            **Score = Budget (0-35) + Urgency (0-25) + Decision authority (0-15) + Fit (0-25)**

            - Any lead identified as a non-buyer (job seeker, student, journalist,
              investor, competitor, recruiter pitch, spam) or with unusable contact
              info is auto-disqualified before scoring.
            - **Contact Now**: score ≥ 65
            - **Nurture**: score 35-64
            - **Disqualify**: score < 35
            """
        )
else:
    st.info("Upload a CSV to run the triage system.")
