"""
Lead Triage Pipeline
=====================
Turns a messy inbound-lead CSV export into a ranked, reusable output:
Contact Now / Nurture / Disqualify, with a transparent score and reason
for every row.

Run against ANY future export with the same rough column set - nothing
here is hardcoded to this specific file.

Pipeline stages:
    1. load_raw          - defensively read a messy CSV
    2. normalize          - standardize id/date/employees/budget/email
    3. drop_structural_junk - blank rows, test rows, embedded header rows
    4. deduplicate        - collapse repeat submissions
    5. classify_notes     - read free-text notes, tag lead type + signals
    6. score              - transparent 0-100 rubric
    7. bucket             - Contact Now / Nurture / Disqualify
"""

import re
import io
import pandas as pd
import numpy as np


EXPECTED_COLS = [
    "lead_id", "created", "name", "email", "company", "employees",
    "website", "title", "source", "monthly_budget", "notes",
]

# ---------------------------------------------------------------------
# 1. LOAD
# ---------------------------------------------------------------------

def load_raw(path_or_buffer) -> pd.DataFrame:
    """Read the CSV defensively. Keeps everything as string initially -
    numeric/date parsing happens explicitly in normalize() so we control
    every conversion instead of letting pandas guess."""
    df = pd.read_csv(path_or_buffer, dtype=str, keep_default_na=False)
    df.columns = [c.strip().lower() for c in df.columns]
    # Some exports may be missing a column or have extras - align to
    # the expected schema so downstream code never KeyErrors.
    for col in EXPECTED_COLS:
        if col not in df.columns:
            df[col] = ""
    return df[EXPECTED_COLS].copy()


# ---------------------------------------------------------------------
# 2. NORMALIZE
# ---------------------------------------------------------------------

def normalize_lead_id(raw_id: str, row_idx: int) -> str:
    raw_id = (raw_id or "").strip()
    if not raw_id:
        return f"AUTO-{row_idx}"
    raw_id = raw_id.replace("L-", "").replace("l-", "")
    raw_id = raw_id.replace("-dup", "").strip()
    return f"L-{raw_id}" if raw_id.isdigit() else raw_id


DATE_FORMATS = [
    "%m/%d/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m-%d-%Y",
    "%b %d %Y", "%m/%d/%y", "%Y-%m-%d",
]

def normalize_date(raw_date: str):
    raw_date = (raw_date or "").strip()
    if not raw_date:
        return pd.NaT
    raw_date = raw_date.replace(",", "")
    for fmt in DATE_FORMATS:
        try:
            return pd.to_datetime(raw_date, format=fmt)
        except (ValueError, TypeError):
            continue
    # last resort: let pandas guess, dayfirst=False (matches majority format)
    try:
        return pd.to_datetime(raw_date, errors="coerce", dayfirst=False)
    except Exception:
        return pd.NaT


def normalize_employees(raw: str):
    """'35-55' -> 45 (midpoint) | '19+' -> 19 | '~43' -> 43 | '' -> NaN"""
    raw = (raw or "").strip().replace("~", "")
    if not raw:
        return np.nan
    if "-" in raw:
        parts = raw.split("-")
        try:
            nums = [float(p) for p in parts if p.strip().isdigit()]
            return sum(nums) / len(nums) if nums else np.nan
        except ValueError:
            return np.nan
    raw = raw.replace("+", "")
    try:
        return float(raw)
    except ValueError:
        return np.nan


def normalize_budget(raw: str):
    """Returns an estimated MONTHLY USD budget as a float, or NaN if
    genuinely unstated ('TBD', 'depends', blank)."""
    raw = (raw or "").strip().lower()
    if not raw or raw in ("tbd", "depends", "n/a", "none"):
        return np.nan
    raw = raw.replace("$", "").replace(",", "").replace("/mo", "")
    if "-" in raw:
        parts = raw.split("-")
        vals = [_to_number(p) for p in parts]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else np.nan
    val = _to_number(raw)
    return val if val is not None else np.nan


def _to_number(token: str):
    token = token.strip().replace("k", "000")
    try:
        return float(token)
    except ValueError:
        return None


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def normalize_email(raw: str) -> str:
    raw = (raw or "").strip()
    raw = raw.replace("[at]", "@")
    return raw


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email or ""))


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["lead_id"] = [normalize_lead_id(v, i) for i, v in enumerate(df["lead_id"])]
    df["created_date"] = df["created"].apply(normalize_date)
    df["employees_n"] = df["employees"].apply(normalize_employees)
    df["monthly_budget_usd"] = df["monthly_budget"].apply(normalize_budget)
    df["email"] = df["email"].apply(normalize_email)
    df["email_valid"] = df["email"].apply(is_valid_email)
    df["name"] = df["name"].str.strip()
    df["company"] = df["company"].str.strip()
    df["notes"] = df["notes"].str.strip()
    df["notes_lower"] = df["notes"].str.lower()
    return df


# ---------------------------------------------------------------------
# 3. DROP STRUCTURAL JUNK
# ---------------------------------------------------------------------

def drop_structural_junk(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Removes rows that aren't leads at all: blank rows, an embedded
    header row re-sent as data, and obvious QA/test rows. Returns
    (clean_df, removed_df) so removed rows stay auditable rather than
    silently vanishing."""
    reasons = pd.Series("", index=df.index)

    blank_mask = (df["email"].str.strip() == "") & (df["name"].str.strip() == "") & (df["notes"].str.strip() == "")
    reasons[blank_mask] = "structural: completely blank row"

    header_mask = df["lead_id"].str.lower().eq("header")
    reasons[header_mask] = "structural: embedded header row"

    test_mask = df["notes_lower"].str.contains("qa test entry", na=False) | \
                (df["lead_id"].str.upper() == "TESTROW") | \
                (df["email"].str.lower() == "test@test.com") | \
                (df["name"].str.lower() == "asdf")
    reasons[test_mask] = "structural: test/QA row"

    remove_mask = blank_mask | header_mask | test_mask
    removed = df[remove_mask].copy()
    removed["removed_reason"] = reasons[remove_mask]
    kept = df[~remove_mask].copy()
    return kept, removed


# ---------------------------------------------------------------------
# 4. DEDUPLICATE
# ---------------------------------------------------------------------

def deduplicate(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Same email + same company = same lead, regardless of whether the
    original had a '-dup' tag or a '(duplicate submission)' note (many
    silent duplicates in this export had neither). Keeps the most
    RECENT submission and logs the rest as duplicates."""
    df = df.copy()
    df["dedupe_key"] = (df["email"].str.lower().str.strip() + "|" +
                         df["company"].str.lower().str.strip())

    df_sorted = df.sort_values("created_date", na_position="first")
    is_dup = df_sorted.duplicated(subset="dedupe_key", keep="last")
    # never treat rows with a blank dedupe key (blank email+company) as duplicates of each other
    blank_key = df_sorted["dedupe_key"].isin(["|", ""])
    is_dup = is_dup & ~blank_key

    duplicates = df_sorted[is_dup].copy()
    duplicates["removed_reason"] = "duplicate submission (kept most recent)"
    kept = df_sorted[~is_dup].copy()
    return kept, duplicates


# ---------------------------------------------------------------------
# 5. CLASSIFY NOTES (free-text signal extraction)
# ---------------------------------------------------------------------

NON_BUYER_PATTERNS = {
    "job_seeker": [
        r"looking for a role", r"attaching my cv", r"are you hiring",
        r"love to join your team",
    ],
    "student_or_learner": [
        r"cs student", r"final year student", r"university project",
        r"bootcamp grad", r"not looking to buy.*just learning",
    ],
    "journalist": [r"journalist writing about"],
    "investor": [r"^vc here", r"portfolio companies", r"not a direct buyer"],
    "competitor_research": [
        r"competing automation agency", r"fellow agency owner here",
        r"curious about your pricing for benchmarking",
    ],
    "recruiter_or_staffing_pitch": [r"automation devs on our bench", r"place candidates"],
    "spam_or_scam": [
        r"won \$", r"click here to claim", r"smm panel", r"buy followers",
        r"high-da backlinks", r"rank #1 guaranteed", r"offshore dev team",
        r"bulk email blasting",
    ],
    "misfiled_signup": [r"newsletter signup that ended up"],
}

WEAK_PROSPECT_PATTERNS = {
    "no_budget_yet": [
        r"can'?t really pay right now", r"tiny budget", r"no real budget yet",
    ],
    "off_icp_small_biz": [r"small local business, not an agency"],
}

ADJACENT_ICP_PATTERNS = {
    "saas_not_agency": [r"saas company \(not an agency\)"],
    "ecom_embedded_dev": [r"ecom brand, not an agency"],
    "car_dealership": [r"car dealership"],
}

UNREACHABLE_PATTERNS = [r"weird-email-no-domain", r"broken email", r"email is garbage"]

BUDGET_APPROVED_RE = re.compile(r"budget approved")
PRICE_SENSITIVE_RE = re.compile(r"price sensitive")
BUDGET_NOT_LOCKED_RE = re.compile(r"budget not locked yet")
WONT_SHARE_BUDGET_RE = re.compile(r"won'?t share budget")
VAGUE_SCOPE_RE = re.compile(r"interested but vague on scope")
NOT_SURE_SIGNOFF_RE = re.compile(r"not sure who signs off")
DECISION_MINE_RE = re.compile(r"decision is mine|i make the call here")
LOOP_IN_TEAM_RE = re.compile(r"would need to loop in the team")
COMPARING_RE = re.compile(r"comparing a few options")
NOT_SURE_NEED_RE = re.compile(r"not totally sure what we need yet")

URGENCY_HIGH_RE = re.compile(
    r"wants to start asap|keen to move fast|ready to pilot in the next 2 weeks|"
    r"want to start this month|wants to move in 2 weeks|"
    r"this is my priority to solve|this is a priority for the quarter|"
    r"decision this month"
)
URGENCY_MED_RE = re.compile(r"decision in about a month")

AGENCY_TYPE_RE = re.compile(r"we'?re an? ([a-z\- ]+?) agency")
EMPLOYEE_MENTION_RE = re.compile(r"(\d+)\s+people")


def _any_match(patterns, text):
    return any(re.search(p, text) for p in patterns)


def classify_notes(notes_lower: str) -> dict:
    result = {
        "lead_type": "prospect",     # prospect | weak_prospect | adjacent_icp | non_buyer | unreachable
        "disqualify_reason": "",
        "budget_approved": bool(BUDGET_APPROVED_RE.search(notes_lower)),
        "price_sensitive": bool(PRICE_SENSITIVE_RE.search(notes_lower)),
        "budget_not_locked": bool(BUDGET_NOT_LOCKED_RE.search(notes_lower) or WONT_SHARE_BUDGET_RE.search(notes_lower)),
        "vague_scope": bool(VAGUE_SCOPE_RE.search(notes_lower)),
        "decision_authority": bool(DECISION_MINE_RE.search(notes_lower)),
        "signoff_unclear": bool(NOT_SURE_SIGNOFF_RE.search(notes_lower) or LOOP_IN_TEAM_RE.search(notes_lower)),
        "urgency": "high" if URGENCY_HIGH_RE.search(notes_lower)
                   else "medium" if URGENCY_MED_RE.search(notes_lower)
                   else "low",
        "comparing_options": bool(COMPARING_RE.search(notes_lower) or NOT_SURE_NEED_RE.search(notes_lower)),
        "agency_type": None,
        "notes_employee_mention": None,
    }

    m = AGENCY_TYPE_RE.search(notes_lower)
    if m:
        result["agency_type"] = m.group(1).strip()
    m2 = EMPLOYEE_MENTION_RE.search(notes_lower)
    if m2:
        result["notes_employee_mention"] = int(m2.group(1))

    if any(re.search(p, notes_lower) for p in UNREACHABLE_PATTERNS):
        result["lead_type"] = "unreachable"
        result["disqualify_reason"] = "unusable contact info"
        return result

    for reason, patterns in NON_BUYER_PATTERNS.items():
        if _any_match(patterns, notes_lower):
            result["lead_type"] = "non_buyer"
            result["disqualify_reason"] = reason.replace("_", " ")
            return result

    for reason, patterns in ADJACENT_ICP_PATTERNS.items():
        if _any_match(patterns, notes_lower):
            result["lead_type"] = "adjacent_icp"
            return result

    for reason, patterns in WEAK_PROSPECT_PATTERNS.items():
        if _any_match(patterns, notes_lower):
            result["lead_type"] = "weak_prospect"
            return result

    return result


# ---------------------------------------------------------------------
# 6. SCORE  (transparent 0-100 rubric)
# ---------------------------------------------------------------------

def score_row(row: dict) -> dict:
    c = row["_classification"]

    if c["lead_type"] in ("non_buyer", "unreachable"):
        return {"score": 0, "bucket": "Disqualify",
                "reason": c["disqualify_reason"] or c["lead_type"]}

    budget = row.get("monthly_budget_usd")
    employees = row.get("employees_n") or c.get("notes_employee_mention")

    # --- Budget component (0-35) ---
    if pd.isna(budget):
        budget_pts = 5
    elif budget >= 10000:
        budget_pts = 35
    elif budget >= 5000:
        budget_pts = 25
    elif budget >= 2000:
        budget_pts = 15
    elif budget > 0:
        budget_pts = 8
    else:
        budget_pts = 2
    if c["budget_approved"]:
        budget_pts = min(35, budget_pts + 10)
    if c["budget_not_locked"] or c["price_sensitive"]:
        budget_pts = max(0, budget_pts - 8)

    # --- Urgency component (0-25) ---
    urgency_pts = {"high": 25, "medium": 12, "low": 5}[c["urgency"]]
    if c["comparing_options"]:
        urgency_pts = min(urgency_pts, 8)

    # --- Authority component (0-15) ---
    if c["decision_authority"]:
        authority_pts = 15
    elif c["signoff_unclear"]:
        authority_pts = 5
    else:
        authority_pts = 9  # neutral - not stated either way

    # --- Fit component (0-25) ---
    if c["lead_type"] == "adjacent_icp":
        fit_pts = 15
    elif c["lead_type"] == "weak_prospect":
        fit_pts = 6
    else:
        fit_pts = 22  # core marketing/growth-agency ICP
        if employees and employees >= 10:
            fit_pts = 25
        if c["vague_scope"]:
            fit_pts -= 5

    total = budget_pts + urgency_pts + authority_pts + fit_pts
    total = max(0, min(100, round(total)))

    if total >= 65:
        bucket = "Contact Now"
    elif total >= 35:
        bucket = "Nurture"
    else:
        bucket = "Disqualify"

    reason_bits = []
    if c["budget_approved"]:
        reason_bits.append("budget approved")
    if c["urgency"] == "high":
        reason_bits.append("high urgency")
    if c["decision_authority"]:
        reason_bits.append("clear decision authority")
    if not reason_bits:
        reason_bits.append(f"{c['lead_type']}, score {total}/100")

    return {"score": total, "bucket": bucket, "reason": "; ".join(reason_bits)}


# ---------------------------------------------------------------------
# 7. FULL PIPELINE
# ---------------------------------------------------------------------

def run_pipeline(path_or_buffer):
    raw = load_raw(path_or_buffer)
    norm = normalize(raw)
    kept, structural_removed = drop_structural_junk(norm)
    kept, dup_removed = deduplicate(kept)

    kept["_classification"] = kept["notes_lower"].apply(classify_notes)

    scored = kept.apply(lambda r: score_row(r.to_dict()), axis=1, result_type="expand")
    kept = pd.concat([kept.reset_index(drop=True), scored.reset_index(drop=True)], axis=1)

    # flatten a couple of classification fields for display
    kept["lead_type"] = kept["_classification"].apply(lambda c: c["lead_type"])
    kept["urgency"] = kept["_classification"].apply(lambda c: c["urgency"])
    kept["budget_approved"] = kept["_classification"].apply(lambda c: c["budget_approved"])

    output_cols = [
        "lead_id", "name", "email", "company", "employees_n", "monthly_budget_usd",
        "title", "source", "created_date", "lead_type", "urgency", "budget_approved",
        "score", "bucket", "reason",
    ]
    ranked = kept[output_cols].sort_values(["bucket", "score"],
                                            key=lambda s: s if s.name != "bucket"
                                            else s.map({"Contact Now": 0, "Nurture": 1, "Disqualify": 2}),
                                            ascending=[True, False])

    audit_removed = pd.concat([structural_removed, dup_removed], ignore_index=True, sort=False)

    summary = ranked["bucket"].value_counts().to_dict()
    return ranked.reset_index(drop=True), audit_removed, summary


if __name__ == "__main__":
    ranked, removed, summary = run_pipeline("leads_raw.csv")
    print("=== SUMMARY ===")
    print(summary)
    ranked.to_csv("ranked_leads.csv", index=False)
    removed.to_csv("removed_rows_audit.csv", index=False)
    print(f"\nWrote {len(ranked)} ranked leads to ranked_leads.csv")
    print(f"Wrote {len(removed)} audited removals to removed_rows_audit.csv")
