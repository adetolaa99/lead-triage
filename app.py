import streamlit as st
import pandas as pd
from pipeline import run_pipeline

st.set_page_config(page_title="Lead Triage", layout="wide")

st.title("Lead Triage System")
st.caption(
    "Upload a lead export (.csv) with columns: lead_id, created, name, email, "
    "company, employees, website, title, source, monthly_budget, notes. "
    "The system cleans it, reads every note, scores intent + fit, and ranks "
    "every lead into Contact Now / Nurture / Disqualify."
)

uploaded = st.file_uploader("Upload lead export (.csv)", type=["csv"])

if uploaded:
    with st.spinner("Cleaning data, reading notes, scoring leads..."):
        ranked, removed, summary = run_pipeline(uploaded)

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
        st.caption("This is a summary view (5 columns) so it fits on screen without side-scrolling. "
                   "Use the 'Inspect a single lead' tab to see everything about one lead.")
        st.dataframe(view, use_container_width=True, height=450, hide_index=True)

        st.download_button(
            "Download full ranked leads (CSV, all columns)",
            ranked.to_csv(index=False).encode("utf-8"),
            file_name="ranked_leads.csv",
            mime="text/csv",
        )

    # ---------------- TAB 2: single-lead inspector ----------------
    with tab_inspect:
        st.caption("Pick one lead to see its original note, side by side with exactly how it scored.")
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
            st.markdown("**Original note (verbatim from the export):**")
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
        st.caption("Rows removed BEFORE scoring even started — blank rows, test rows, and duplicate "
                   "submissions. Nothing here was scored or judged; it just isn't a usable, unique lead.")
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
            ### How every lead gets a score

            **Score = Budget (0–35) + Urgency (0–25) + Decision authority (0–15) + Fit (0–25)**

            Before scoring, any lead identified as a **non-buyer** — job seeker, student,
            journalist, investor, competitor doing research, recruiter pitch, or spam — is
            disqualified immediately with the reason recorded. Everyone else gets scored:

            - **Budget**: parsed from the budget field, boosted if the notes say
              "budget approved," reduced if the notes say "price sensitive" or
              "budget not locked yet."
            - **Urgency**: language like "wants to start ASAP" or "ready to pilot in the
              next 2 weeks" scores high; "comparing a few options" scores low.
            - **Decision authority**: "decision is mine" scores high; "not sure who signs
              off internally" scores low.
            - **Fit**: does the lead match the core client profile (a marketing/growth
              agency with a described operational pain point), or is it adjacent
              (a budgeted-but-different type of business) or weak (interest, no real
              budget)?

            **Thresholds:** score ≥ 65 → Contact Now · 35–64 → Nurture · below 35 → Disqualify
            """
        )
else:
    st.info("Upload a CSV to run the triage system.")
