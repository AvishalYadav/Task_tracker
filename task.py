"""
================================================================================
 TASK TRACKER  -  Streamlit application
================================================================================
 Author-ready, enterprise-grade single-file app.

 Features
   - Shared-password gate (via st.secrets)
   - Full CRUD (Create / Read / Update / Delete) on tasks
   - Swappable storage backend (CSV for local dev, Google Sheets for publishing)
   - Dashboard with multiple interactive charts (Plotly)
   - Data explorer with filters + download ("curate necessary information")

 Storage architecture (Repository pattern)
   The UI talks ONLY to a `TaskRepository`. Two implementations are provided:
     * CSVRepository      -> local development (fast, no setup)
     * GSheetsRepository  -> persistent storage for Streamlit Community Cloud
   Switch by changing STORAGE_BACKEND below.  Nothing else changes.

 IMPORTANT for publishing
   Streamlit Community Cloud does NOT persist local files (CSV/SQLite) across
   reboots or redeploys.  Use STORAGE_BACKEND = "gsheets" (or a hosted DB) once
   you deploy.  Keep "csv" only for local work.
================================================================================
"""

import os
import json
import datetime
import numpy as np
import pandas as pd
import streamlit as st
import plotly.express as px
import streamlit.components.v1 as components

# ------------------------------------------------------------------ #
#  1. CONFIGURATION
# ------------------------------------------------------------------ #

# "gsheets" -> Google Sheets (persists locally AND on Community Cloud)  <-- your choice
# "csv"     -> local-only CSV fallback (not persistent when published)
STORAGE_BACKEND = "gsheets"

# Your preferred local path. Falls back to a relative file if the folder
# does not exist (e.g. when running on a Linux host / the cloud).
PREFERRED_CSV_PATH = r"C:\Cursor\Task_Tracker\Database.csv"
CSV_PATH = (
    PREFERRED_CSV_PATH
    if os.path.isdir(os.path.dirname(PREFERRED_CSV_PATH))
    else "Database.csv"
)

GSHEETS_WORKSHEET = "Tasks"          # tab name; auto-created if missing
DEFAULT_PASSWORD = "admin"           # used ONLY if no secret is configured

# --- Google Sheets configuration ---------------------------------- #
SPREADSHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1pwb9UqDZUkp_28yxiOZaFtz154QU3YPtTpyT0OWstSE/edit"
)
# Local: read the service-account key directly from this file.
# On Community Cloud (file absent), credentials are read from st.secrets
# under the [gcp_service_account] block instead.
SERVICE_ACCOUNT_JSON_PATH = r"C:\Cursor\Task_Tracker\curious-drive-471111-v1-29f8903f3aeb.json"
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Optional: corporate networks that inspect HTTPS may present a certificate
# signed by an internal CA that Python does not trust by default. Preferred fix
# is to `pip install pip-system-certs`. If that is blocked, export your
# corporate root CA to a .pem and set its path here to trust it explicitly.
CA_BUNDLE_PATH = r""          # e.g. r"C:\Cursor\Task_Tracker\corp_root_ca.pem"
if CA_BUNDLE_PATH:
    os.environ.setdefault("SSL_CERT_FILE", CA_BUNDLE_PATH)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", CA_BUNDLE_PATH)

# Canonical schema -- the single source of truth for column order.
COLUMNS = [
    "Task_ID", "Title", "Description", "Project", "Assignee",
    "Priority", "Status", "Progress_Pct",
    "Created_Date", "Due_Date", "Completed_Date",
    "Estimated_Hours", "Actual_Hours", "Comment",
    "Created_By", "Updated_By", "Updated_At",
]

STATUS_OPTIONS = ["Backlog", "Planned", "To Do", "In Progress",
                  "Testing in Progress", "Testing Completed", "On Hold",
                  "Blocked", "Completed", "Cancelled"]
PRIORITY_OPTIONS = ["Low", "Medium", "High", "Critical"]
DONE_STATUSES = ["Completed"]                 # count toward Completion %
CLOSED_STATUSES = ["Completed", "Cancelled"]  # terminal: not open, not overdue


# ------------------------------------------------------------------ #
#  2. STORAGE LAYER  (Repository pattern)
# ------------------------------------------------------------------ #

def _stringify(record):
    """CSV/Sheets store everything as text, so coerce all values to str."""
    return {k: ("" if v is None else str(v)) for k, v in record.items()}


class CSVRepository:
    """Reads/writes tasks to a local CSV file. For local development only."""

    def __init__(self, path):
        self.path = path
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        if not os.path.exists(path):
            pd.DataFrame(columns=COLUMNS).to_csv(path, index=False)

    def load_all(self):
        df = pd.read_csv(self.path, dtype=str).fillna("")
        for col in COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[COLUMNS]

    def _write(self, df):
        # Atomic-ish write: write to temp, then replace, to avoid corruption.
        tmp = self.path + ".tmp"
        df[COLUMNS].to_csv(tmp, index=False)
        os.replace(tmp, self.path)

    def add(self, record):
        record = _stringify(record)
        df = self.load_all()
        df = pd.concat([df, pd.DataFrame([record])], ignore_index=True)
        self._write(df)

    def update(self, task_id, record):
        record = _stringify(record)
        df = self.load_all()
        mask = df["Task_ID"] == task_id
        for key, value in record.items():
            df.loc[mask, key] = value
        self._write(df)

    def delete(self, task_id):
        df = self.load_all()
        df = df[df["Task_ID"] != task_id]
        self._write(df)

    def add_many(self, records):
        if not records:
            return
        df = self.load_all()
        rows = pd.DataFrame([_stringify(r) for r in records])
        for col in COLUMNS:
            if col not in rows.columns:
                rows[col] = ""
        df = pd.concat([df, rows[COLUMNS]], ignore_index=True)
        self._write(df)


@st.cache_resource(show_spinner="Connecting to Google Sheets...")
def _get_worksheet():
    """
    Authorise with gspread and return the 'Tasks' worksheet handle.
    Credentials: local JSON key file if present, else Streamlit secrets (cloud).
    Auto-creates the worksheet + header row if they don't exist yet.
    Cached so the auth handshake runs once, not on every rerun.
    """
    import gspread
    from google.oauth2.service_account import Credentials

    if os.path.exists(SERVICE_ACCOUNT_JSON_PATH):
        creds = Credentials.from_service_account_file(
            SERVICE_ACCOUNT_JSON_PATH, scopes=GOOGLE_SCOPES
        )
    else:
        # Community Cloud: paste the JSON under [gcp_service_account] in Secrets
        info = dict(st.secrets["gcp_service_account"])
        creds = Credentials.from_service_account_info(info, scopes=GOOGLE_SCOPES)

    gc = gspread.authorize(creds)
    sh = gc.open_by_url(SPREADSHEET_URL)
    try:
        ws = sh.worksheet(GSHEETS_WORKSHEET)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(
            title=GSHEETS_WORKSHEET, rows=1000, cols=len(COLUMNS)
        )
    # Ensure row 1 holds the canonical header. Rewriting only row 1 preserves
    # the data rows below and auto-migrates renamed columns (e.g. Tags->Comment).
    values = ws.get_all_values()
    first_row = values[0] if values else []
    if list(first_row[:len(COLUMNS)]) != COLUMNS:
        ws.update([COLUMNS], "A1")
    return ws


class GSheetsRepository:
    """
    Reads/writes tasks to a Google Sheet via gspread + a service account.
    Works identically for local (JSON file) and Community Cloud (secrets).
    """

    def __init__(self, worksheet=GSHEETS_WORKSHEET):
        self.worksheet = worksheet
        self.ws = _get_worksheet()

    def load_all(self):
        # Build from raw values so a header mismatch never crashes the app.
        values = self.ws.get_all_values()
        if not values or "Task_ID" not in values[0]:
            return pd.DataFrame(columns=COLUMNS)
        header = values[0]
        df = pd.DataFrame(values[1:], columns=header)
        for col in COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[COLUMNS].fillna("").astype(str)

    def _rewrite(self, df):
        data = [COLUMNS] + df[COLUMNS].astype(str).values.tolist()
        self.ws.clear()
        self.ws.update(data, "A1")

    def add(self, record):
        record = _stringify(record)
        self.ws.append_row([record.get(c, "") for c in COLUMNS])

    def update(self, task_id, record):
        record = _stringify(record)
        df = self.load_all()
        mask = df["Task_ID"] == task_id
        for key, value in record.items():
            df.loc[mask, key] = value
        self._rewrite(df)

    def delete(self, task_id):
        df = self.load_all()
        df = df[df["Task_ID"] != task_id]
        self._rewrite(df)

    def add_many(self, records):
        if not records:
            return
        rows = [[_stringify(r).get(c, "") for c in COLUMNS] for r in records]
        self.ws.append_rows(rows)


def get_repository():
    if STORAGE_BACKEND == "gsheets":
        return GSheetsRepository()
    return CSVRepository(CSV_PATH)


# ------------------------------------------------------------------ #
#  3. AUTH  (single shared password)
# ------------------------------------------------------------------ #

def check_password():
    if st.session_state.get("authenticated"):
        return True

    try:
        configured = st.secrets["app_password"]
    except (KeyError, FileNotFoundError):
        configured = DEFAULT_PASSWORD

    st.markdown(
        """
        <style>
          .hero{background:linear-gradient(120deg,#6366f1 0%,#8b5cf6 45%,#ec4899 100%);
                border-radius:20px;padding:34px 24px;text-align:center;color:#fff;
                box-shadow:0 12px 30px rgba(99,102,241,.35);margin-bottom:6px;}
          .hero h1{font-size:2.4rem;margin:0;font-weight:900;letter-spacing:-.02em;}
          .hero p{font-size:1.05rem;margin:8px 0 0;opacity:.95;}
          .chips{display:flex;flex-wrap:wrap;gap:10px;justify-content:center;margin:16px 0 4px;}
          .chip{padding:8px 14px;border-radius:999px;font-weight:700;font-size:.86rem;color:#fff;}
          .c1{background:#2563eb;} .c2{background:#16a34a;} .c3{background:#f59e0b;}
          .c4{background:#7c3aed;} .c5{background:#dc2626;}
        </style>
        <div class="hero">
          <h1>✅ Task Tracker</h1>
          <p>Plan it, track it, close it — tasks, dashboards & daily action items, all in one place.</p>
        </div>
        <div class="chips">
          <span class="chip c1">📊 Live Dashboard</span>
          <span class="chip c2">🎯 Action Items</span>
          <span class="chip c3">🧮 Pivot Builder</span>
          <span class="chip c4">📸 One-click Copy</span>
          <span class="chip c5">☁️ Google Sheets</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    left, mid, right = st.columns([1, 1.2, 1])
    with mid:
        pwd = st.text_input("🔑 Password", type="password",
                            placeholder="Enter your password")
        if st.button("Login", use_container_width=True, type="primary"):
            if pwd == configured:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("🔒 Password updated. Please contact **Vishal** for access.")
    return False


# ------------------------------------------------------------------ #
#  4. HELPERS
# ------------------------------------------------------------------ #

def next_task_id(df):
    if df.empty:
        return "TSK-0001"
    nums = df["Task_ID"].str.extract(r"(\d+)")[0].dropna()
    n = int(nums.astype(int).max()) + 1 if not nums.empty else 1
    return f"TSK-{n:04d}"


def prepare_analytics(df):
    a = df.copy()
    a["Progress_Pct"] = pd.to_numeric(a["Progress_Pct"], errors="coerce").fillna(0)
    for col in ["Created_Date", "Due_Date", "Completed_Date"]:
        a[col] = pd.to_datetime(a[col], errors="coerce")
    return a


def overdue_mask(a, as_of=None):
    ref = pd.Timestamp(as_of or datetime.date.today())
    return (
        a["Due_Date"].notna()
        & (a["Due_Date"] < ref)
        & (~a["Status"].isin(CLOSED_STATUSES))
    )


# ------------------------------------------------------------------ #
#  4b. COPY-TO-IMAGE + HTML BUILDERS (shared by Dashboard & Pivot)
# ------------------------------------------------------------------ #

# Self-contained HTML/JS: renders the given body, with "Copy image" (to the
# clipboard) and "Download PNG" buttons powered by html2canvas. Placeholders are
# swapped with .replace() (not .format) so the JS braces are left untouched.
_COPYABLE_TEMPLATE = r"""<!DOCTYPE html><html><head><meta charset="utf-8">
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
<style>
  *{box-sizing:border-box;}
  html,body{margin:0;padding:0;background:#ffffff;
    font-family:"Source Sans Pro",system-ui,-apple-system,Segoe UI,sans-serif;color:#0f172a;}
  .bar{display:flex;gap:8px;align-items:center;margin:0 0 10px;}
  .bar button{font:inherit;font-size:0.82rem;font-weight:600;cursor:pointer;
    border:1px solid #e5e7eb;background:#fff;color:#312e81;padding:6px 12px;border-radius:7px;}
  .bar button:hover{filter:brightness(1.05);}
  .bar #msg{font-size:0.8rem;color:#16a34a;font-weight:600;}
  #capture{padding:14px;background:#fff;border-radius:10px;}
  __EXTRA_CSS__
</style></head><body>
<div class="bar">
  <button id="copyBtn">📋 Copy image</button>
  <button id="pngBtn">⬇ Download PNG</button>
  <span id="msg"></span>
</div>
<div id="capture">__BODY__</div>
<script>
const TITLE=__TITLE__;
function flash(t,col){const m=document.getElementById('msg');m.style.color=col||'#16a34a';
  m.textContent=t;setTimeout(()=>{if(m.textContent===t)m.textContent='';},4000);}
function snap(cb){
  if(typeof html2canvas==='undefined'){flash('Image tool blocked by network','#b91c1c');return;}
  const node=document.getElementById('capture');
  html2canvas(node,{backgroundColor:'#ffffff',scale:2,
    windowWidth:node.scrollWidth,windowHeight:node.scrollHeight})
    .then(c=>cb(c)).catch(()=>flash('Could not render image','#b91c1c'));}
document.getElementById('copyBtn').onclick=()=>snap(c=>c.toBlob(async b=>{
  try{await navigator.clipboard.write([new ClipboardItem({'image/png':b})]);
      flash('Copied! Paste with Ctrl+V');}
  catch(e){flash('Clipboard blocked — use Download PNG instead','#b91c1c');}},'image/png'));
document.getElementById('pngBtn').onclick=()=>snap(c=>c.toBlob(b=>{
  const a=document.createElement('a');a.href=URL.createObjectURL(b);
  a.download=TITLE+'.png';a.click();flash('PNG downloaded');},'image/png'));
</script></body></html>"""

_SNAP_CSS = """
.snap-h{font-size:1.05rem;font-weight:800;margin:0 0 12px;color:#0f172a;}
.kpis{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px;}
.kpi{flex:1 1 150px;min-width:140px;border:1px solid #e5e7eb;border-radius:10px;
  padding:10px 12px;background:#fff;}
.kpi-l{font-size:0.70rem;color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:.03em;}
.kpi-v{font-size:1.5rem;font-weight:800;line-height:1.2;}
.bcard{border:1px solid #e5e7eb;border-radius:10px;padding:10px 14px;margin-bottom:12px;background:#fff;}
.btitle{font-weight:700;margin-bottom:8px;color:#334155;}
.brow{display:flex;align-items:center;gap:10px;margin:5px 0;}
.blab{width:140px;font-size:0.82rem;color:#475569;text-align:right;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.btrack{flex:1;background:#f1f5f9;border-radius:6px;height:16px;overflow:hidden;}
.bfill{height:100%;border-radius:6px;}
.bval{width:36px;text-align:right;font-weight:700;font-size:0.82rem;}
.piv{border-collapse:collapse;font-size:0.86rem;}
.piv th,.piv td{border:1px solid #e5e7eb;padding:6px 12px;white-space:nowrap;}
.piv thead th{background:#4338ca;color:#fff;font-weight:700;position:sticky;top:0;}
.piv th.lab,.piv td.lab{text-align:left;}
.piv th.num,.piv td.num{text-align:right;}
.piv tr.total td{background:#e0e7ff;font-weight:700;}
.piv td.totalcol{background:#eef2ff;font-weight:700;}
"""


def copyable_widget(body_html, title="snapshot", height=620):
    """Render arbitrary HTML with Copy-image / Download-PNG controls."""
    html = (_COPYABLE_TEMPLATE
            .replace("__EXTRA_CSS__", _SNAP_CSS)
            .replace("__TITLE__", json.dumps(title))
            .replace("__BODY__", body_html))
    components.html(html, height=height, scrolling=True)


def _esc(v):
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def kpi_cards_html(cards):
    items = "".join(
        f'<div class="kpi"><div class="kpi-l">{_esc(c["label"])}</div>'
        f'<div class="kpi-v" style="color:{c.get("accent", "#0f172a")}">'
        f'{_esc(c["value"])}</div></div>'
        for c in cards
    )
    return f'<div class="kpis">{items}</div>'


def bars_html(title, pairs, color="#2563eb"):
    """pairs: list of (label, value). Renders labelled horizontal CSS bars."""
    pairs = [(l, v) for l, v in pairs if str(l).strip()]
    if not pairs:
        return ""
    mx = max((v for _, v in pairs), default=0) or 1
    rows = "".join(
        f'<div class="brow"><div class="blab">{_esc(l)}</div>'
        f'<div class="btrack"><div class="bfill" '
        f'style="width:{int(100 * v / mx)}%;background:{color}"></div></div>'
        f'<div class="bval">{int(v)}</div></div>'
        for l, v in pairs
    )
    return f'<div class="bcard"><div class="btitle">{_esc(title)}</div>{rows}</div>'


def df_to_html_table(df, label_cols, total_label="Grand Total"):
    label_cols = set(label_cols)
    head = "".join(
        f'<th class="{"lab" if c in label_cols else "num"}">{_esc(c)}</th>'
        for c in df.columns
    )
    body = ""
    for _, row in df.iterrows():
        is_total = any(str(row[c]) == total_label for c in label_cols)
        rcls = ' class="total"' if is_total else ""
        cells = ""
        for c in df.columns:
            ccls = "lab" if c in label_cols else "num"
            if c == total_label:
                ccls += " totalcol"
            cells += f'<td class="{ccls}">{_esc(row[c])}</td>'
        body += f"<tr{rcls}>{cells}</tr>"
    return (f'<table class="piv"><thead><tr>{head}</tr></thead>'
            f'<tbody>{body}</tbody></table>')


# Excel-style "Summarize Values By". (aggfunc, needs_numeric_coercion)
AGGREGATIONS = {
    "Count": ("count", False),
    "Count (Distinct)": ("nunique", False),
    "Sum": ("sum", True),
    "Average": ("mean", True),
    "Min": ("min", False),
    "Max": ("max", False),
    "Std Dev": ("std", True),
    "Variance": ("var", True),
}
PIVOT_PALETTE = ["#2563eb", "#16a34a", "#f59e0b", "#dc2626", "#7c3aed",
                 "#0891b2", "#db2777", "#65a30d", "#ea580c", "#4f46e5"]


# ------------------------------------------------------------------ #
#  5. UI PAGES
# ------------------------------------------------------------------ #

def page_dashboard(df, as_of):
    st.header("📊 Dashboard")
    if df.empty:
        st.info("No tasks yet. Add your first task in the **Add Task** tab.")
        return

    a = prepare_analytics(df)
    od = overdue_mask(a, as_of)
    open_mask = ~a["Status"].isin(CLOSED_STATUSES)

    total = len(a)
    done = int(a["Status"].isin(DONE_STATUSES).sum())
    in_prog = int((a["Status"] == "In Progress").sum())
    overdue = int(od.sum())
    due_today = int(((a["Due_Date"].dt.date == as_of) & open_mask).sum())
    completion = round(100 * done / total, 1) if total else 0.0

    # --- Home banner -----------------------------------------------------
    st.markdown(
        f"""
        <div style="background:linear-gradient(90deg,#eef2ff,#f8fafc);
             border:1px solid #e5e7eb;border-radius:12px;padding:14px 18px;margin-bottom:12px;">
          <span style="font-size:1.05rem;font-weight:700;">👋 Welcome back</span>
          <span style="color:#64748b;"> &nbsp;•&nbsp; {as_of:%A, %d %b %Y}</span><br>
          <span style="color:#334155;">You have
            <b style="color:#dc2626;">{overdue}</b> overdue,
            <b style="color:#2563eb;">{due_today}</b> due today, and
            <b style="color:#f59e0b;">{in_prog}</b> in progress.</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total", total)
    c2.metric("Completed", done)
    c3.metric("In progress", in_prog)
    c4.metric("Due today", due_today)
    c5.metric("Overdue", overdue)
    c6.metric("Completion %", f"{completion}%")

    # --- Chart options (feature 1) --------------------------------------
    with st.expander("⚙️ Chart options — show/hide & change type", expanded=False):
        all_charts = ["Status", "Priority", "Assignee", "Project",
                      "Throughput", "Overdue"]
        show = st.multiselect("Charts to display", all_charts,
                              default=all_charts, key="dash_show")
        oc1, oc2, oc3 = st.columns(3)
        status_kind = oc1.radio("Status", ["Bar", "Donut"], key="dash_status_kind",
                                horizontal=True)
        priority_kind = oc2.radio("Priority", ["Donut", "Bar"], key="dash_prio_kind",
                                  horizontal=True)
        project_kind = oc3.radio("Project", ["Bar", "Donut"], key="dash_proj_kind",
                                 horizontal=True)

    # Reusable data
    status_counts = a["Status"].value_counts().reindex(STATUS_OPTIONS).fillna(0)
    priority_counts = a["Priority"].value_counts().reindex(PRIORITY_OPTIONS).fillna(0)
    assignee_counts = (a[a["Assignee"].astype(str).str.strip() != ""]["Assignee"]
                       .value_counts())
    project_counts = (a[a["Project"].astype(str).str.strip() != ""]["Project"]
                      .value_counts())

    def cat_chart(counts, label, kind, palette=None):
        d = counts.reset_index()
        d.columns = [label, "Count"]
        d = d[d["Count"] > 0]
        if d.empty:
            st.caption(f"No {label.lower()} data yet.")
            return
        if kind == "Donut":
            fig = px.pie(d, names=label, values="Count", hole=0.5)
        else:
            fig = px.bar(d, x=label, y="Count", color=label, text="Count",
                         color_discrete_sequence=palette)
        fig.update_layout(margin=dict(t=10, b=0, l=0, r=0), showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    def chart_status():
        st.subheader("Tasks by Status"); cat_chart(status_counts, "Status", status_kind)

    def chart_priority():
        st.subheader("Tasks by Priority"); cat_chart(priority_counts, "Priority", priority_kind)

    def chart_assignee():
        st.subheader("Workload by Assignee")
        w = (a[a["Assignee"].astype(str).str.strip() != ""]
             .groupby(["Assignee", "Status"]).size().reset_index(name="Count"))
        if w.empty:
            st.caption("No assignee data yet."); return
        st.plotly_chart(px.bar(w, x="Count", y="Assignee", color="Status",
                        orientation="h"), use_container_width=True)

    def chart_project():
        st.subheader("Tasks by Project"); cat_chart(project_counts, "Project", project_kind)

    def chart_throughput():
        st.subheader("Throughput over time")
        created = (a.dropna(subset=["Created_Date"])
                   .groupby(a["Created_Date"].dt.date).size().rename("Created"))
        completed = (a.dropna(subset=["Completed_Date"])
                     .groupby(a["Completed_Date"].dt.date).size().rename("Completed"))
        trend = pd.concat([created, completed], axis=1).fillna(0).sort_index()
        if trend.empty:
            st.caption("Add Created / Completed dates to see trends."); return
        trend = trend.reset_index().rename(columns={"index": "Date"})
        trend = trend.melt(id_vars="Date", var_name="Type", value_name="Count")
        st.plotly_chart(px.line(trend, x="Date", y="Count", color="Type",
                        markers=True), use_container_width=True)

    def chart_overdue():
        st.subheader("On-track vs Overdue (open tasks)")
        n_over = overdue
        n_ok = int(open_mask.sum()) - n_over
        donut = pd.DataFrame({"Bucket": ["On track", "Overdue"],
                              "Count": [max(n_ok, 0), n_over]})
        st.plotly_chart(px.pie(donut, names="Bucket", values="Count", hole=0.5,
                        color="Bucket",
                        color_discrete_map={"On track": "#2ca02c", "Overdue": "#d62728"}),
                        use_container_width=True)

    registry = {
        "Status": chart_status, "Priority": chart_priority,
        "Assignee": chart_assignee, "Project": chart_project,
        "Throughput": chart_throughput, "Overdue": chart_overdue,
    }
    selected = [registry[name] for name in all_charts if name in show]
    for i in range(0, len(selected), 2):
        cols = st.columns(2)
        for col, fn in zip(cols, selected[i:i + 2]):
            with col:
                fn()

    # --- Copy the whole dashboard (feature 3) ---------------------------
    st.divider()
    with st.expander("📸 Copy / download the whole dashboard as an image",
                     expanded=False):
        st.caption("Renders the KPIs and distributions into one image you can "
                   "paste into email, Teams, or a doc.")
        cards = [
            {"label": "Total tasks", "value": total, "accent": "#0ea5e9"},
            {"label": "Completed", "value": done, "accent": "#16a34a"},
            {"label": "In progress", "value": in_prog, "accent": "#f59e0b"},
            {"label": "Due today", "value": due_today, "accent": "#2563eb"},
            {"label": "Overdue", "value": overdue, "accent": "#dc2626"},
            {"label": "Completion", "value": f"{completion}%", "accent": "#7c3aed"},
        ]
        body = (f'<div class="snap-h">Task Tracker — Dashboard snapshot '
                f'({as_of:%d %b %Y})</div>')
        body += kpi_cards_html(cards)
        body += bars_html("Tasks by Status",
                          list(status_counts[status_counts > 0].items()), "#2563eb")
        body += bars_html("Tasks by Priority",
                          list(priority_counts[priority_counts > 0].items()), "#7c3aed")
        body += bars_html("Workload by Assignee",
                          list(assignee_counts.head(12).items()), "#0891b2")
        body += bars_html("Tasks by Project",
                          list(project_counts.head(12).items()), "#16a34a")
        copyable_widget(body, title="dashboard_snapshot", height=680)


def page_add(repo, df):
    st.header("➕ Add Task")
    new_id = next_task_id(df)
    st.caption(f"New Task ID: **{new_id}**")

    with st.form("add_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            title = st.text_input("Title *")
            project = st.text_input("Project Code")
            assignee = st.text_input("Assignee")
            priority = st.selectbox("Priority", PRIORITY_OPTIONS, index=1)
            status = st.selectbox("Status", STATUS_OPTIONS,
                                  index=STATUS_OPTIONS.index("To Do"))
            progress = st.slider("Progress %", 0, 100, 0)
        with c2:
            due = st.date_input("Due date", value=datetime.date.today())
            est = st.number_input("Estimated hours", min_value=0.0, step=0.5)
            act = st.number_input("Actual hours", min_value=0.0, step=0.5)
            comment = st.text_input("Comment")
            created_by = st.text_input("Created by")
        description = st.text_area("Description")

        submitted = st.form_submit_button("Create task")
        if submitted:
            if not title.strip():
                st.error("Title is required.")
            else:
                now = datetime.datetime.now().isoformat(timespec="seconds")
                record = {
                    "Task_ID": new_id,
                    "Title": title.strip(),
                    "Description": description.strip(),
                    "Project": project.strip(),
                    "Assignee": assignee.strip(),
                    "Priority": priority,
                    "Status": status,
                    "Progress_Pct": progress,
                    "Created_Date": datetime.date.today().isoformat(),
                    "Due_Date": due.isoformat(),
                    "Completed_Date": (
                        datetime.date.today().isoformat()
                        if status == "Completed" else ""
                    ),
                    "Estimated_Hours": est,
                    "Actual_Hours": act,
                    "Comment": comment.strip(),
                    "Created_By": created_by.strip(),
                    "Updated_By": created_by.strip(),
                    "Updated_At": now,
                }
                repo.add(record)
                st.success(f"Task {new_id} created.")
                st.rerun()

    _bulk_import_section(repo, df)


def _bulk_import_section(repo, df):
    st.divider()
    with st.expander("📥 Bulk import — add many tasks from CSV / Excel"):
        recognised = [c for c in COLUMNS
                      if c not in ("Task_ID", "Created_By", "Updated_By", "Updated_At")]
        st.caption("Upload a file with at least a **Title** column. Recognised "
                   "headers (case-insensitive): " + ", ".join(recognised) + ". "
                   "Task IDs, timestamps and blank Status/Priority are filled "
                   "automatically.")
        up = st.file_uploader("CSV or Excel", type=["csv", "xlsx"], key="bulk_upload")
        if up is None:
            return
        try:
            raw = (pd.read_csv(up) if up.name.lower().endswith(".csv")
                   else pd.read_excel(up))
        except Exception as exc:
            st.error(f"Could not read the file: {exc}"); return

        norm = {str(c).strip().casefold(): c for c in raw.columns}
        if "title" not in norm:
            st.error("The file must contain a **Title** column."); return

        mapped = pd.DataFrame(index=raw.index)
        for col in COLUMNS:
            src = norm.get(col.casefold())
            mapped[col] = raw[src].astype(str).values if src else ""
        mapped = mapped[mapped["Title"].astype(str).str.strip() != ""]
        if mapped.empty:
            st.warning("No rows with a Title were found."); return

        st.write(f"**{len(mapped)}** valid row(s) ready to import. Preview:")
        st.dataframe(mapped.head(20), hide_index=True, use_container_width=True)

        if st.button(f"✅ Import {len(mapped)} task(s)", type="primary"):
            start_num = 0
            if not df.empty:
                nums = df["Task_ID"].str.extract(r"(\d+)")[0].dropna()
                start_num = int(nums.astype(int).max()) if not nums.empty else 0
            now = datetime.datetime.now().isoformat(timespec="seconds")
            today = datetime.date.today().isoformat()
            records = []
            for i, (_, r) in enumerate(mapped.iterrows(), start=1):
                rec = {c: str(r[c]) for c in COLUMNS}
                rec["Task_ID"] = f"TSK-{start_num + i:04d}"
                if rec.get("Status", "").strip() not in STATUS_OPTIONS:
                    rec["Status"] = "To Do"
                if rec.get("Priority", "").strip() not in PRIORITY_OPTIONS:
                    rec["Priority"] = "Medium"
                if not rec.get("Created_Date", "").strip():
                    rec["Created_Date"] = today
                rec["Updated_At"] = now
                records.append(rec)
            repo.add_many(records)
            st.success(f"Imported {len(records)} task(s).")
            st.rerun()


def page_update_delete(repo, df):
    st.header("✏️ Update / Delete")
    if df.empty:
        st.info("No tasks to edit yet.")
        return

    labels = (df["Task_ID"] + " — " + df["Title"]).tolist()
    picked = st.selectbox("Select a task", labels)
    task_id = picked.split(" — ")[0]
    row = df[df["Task_ID"] == task_id].iloc[0]

    with st.form("edit_form"):
        c1, c2 = st.columns(2)
        with c1:
            title = st.text_input("Title *", row["Title"])
            project = st.text_input("Project Code", row["Project"])
            assignee = st.text_input("Assignee", row["Assignee"])
            priority = st.selectbox(
                "Priority", PRIORITY_OPTIONS,
                index=PRIORITY_OPTIONS.index(row["Priority"])
                if row["Priority"] in PRIORITY_OPTIONS else 1,
            )
            status = st.selectbox(
                "Status", STATUS_OPTIONS,
                index=STATUS_OPTIONS.index(row["Status"])
                if row["Status"] in STATUS_OPTIONS
                else STATUS_OPTIONS.index("To Do"),
            )
            progress = st.slider(
                "Progress %", 0, 100,
                int(float(row["Progress_Pct"])) if str(row["Progress_Pct"]).strip() else 0,
            )
        with c2:
            due_val = pd.to_datetime(row["Due_Date"], errors="coerce")
            due = st.date_input(
                "Due date",
                value=due_val.date() if pd.notna(due_val) else datetime.date.today(),
            )
            est = st.number_input(
                "Estimated hours", min_value=0.0, step=0.5,
                value=float(row["Estimated_Hours"]) if str(row["Estimated_Hours"]).strip() else 0.0,
            )
            act = st.number_input(
                "Actual hours", min_value=0.0, step=0.5,
                value=float(row["Actual_Hours"]) if str(row["Actual_Hours"]).strip() else 0.0,
            )
            comment = st.text_input("Comment", row["Comment"])
            updated_by = st.text_input("Updated by", row["Updated_By"])
        description = st.text_area("Description", row["Description"])

        save = st.form_submit_button("💾 Save changes")

    if save:
        if not title.strip():
            st.error("Title is required.")
        else:
            now = datetime.datetime.now().isoformat(timespec="seconds")
            completed = row["Completed_Date"]
            if status == "Completed" and not str(completed).strip():
                completed = datetime.date.today().isoformat()
            if status != "Completed":
                completed = ""
            repo.update(task_id, {
                "Title": title.strip(),
                "Description": description.strip(),
                "Project": project.strip(),
                "Assignee": assignee.strip(),
                "Priority": priority,
                "Status": status,
                "Progress_Pct": progress,
                "Due_Date": due.isoformat(),
                "Completed_Date": completed,
                "Estimated_Hours": est,
                "Actual_Hours": act,
                "Comment": comment.strip(),
                "Updated_By": updated_by.strip(),
                "Updated_At": now,
            })
            st.success(f"Task {task_id} updated.")
            st.rerun()

    st.divider()
    st.subheader("🗑️ Delete task")
    confirm = st.checkbox(f"Yes, permanently delete {task_id}")
    if st.button("Delete", type="primary", disabled=not confirm):
        repo.delete(task_id)
        st.success(f"Task {task_id} deleted.")
        st.rerun()


def page_explore(df):
    st.header("🔎 Explore & Curate Data")
    if df.empty:
        st.info("No data to explore yet.")
        return

    search = st.text_input("🔍 Search",
                           placeholder="Search title, project code, assignee, comment…")
    c1, c2, c3 = st.columns(3)
    with c1:
        f_status = st.multiselect("Status", STATUS_OPTIONS)
    with c2:
        f_priority = st.multiselect("Priority", PRIORITY_OPTIONS)
    with c3:
        assignees = sorted([x for x in df["Assignee"].unique() if str(x).strip()])
        f_assignee = st.multiselect("Assignee", assignees)

    filtered = df.copy()
    if search.strip():
        q = search.strip().casefold()
        scols = ["Task_ID", "Title", "Description", "Project", "Assignee", "Comment"]
        hay = filtered[scols].astype(str).agg(" ".join, axis=1).str.casefold()
        filtered = filtered[hay.str.contains(q, regex=False, na=False)]
    if f_status:
        filtered = filtered[filtered["Status"].isin(f_status)]
    if f_priority:
        filtered = filtered[filtered["Priority"].isin(f_priority)]
    if f_assignee:
        filtered = filtered[filtered["Assignee"].isin(f_assignee)]

    keep = st.multiselect("Columns to show", COLUMNS, default=COLUMNS)
    view = filtered[keep] if keep else filtered

    # Pagination for large lists
    total = len(view)
    pc1, pc2 = st.columns([1, 3])
    page_size = pc1.selectbox("Rows per page", [25, 50, 100, 200], index=0)
    pages = max(1, (total + page_size - 1) // page_size)
    page = pc2.number_input(f"Page (1–{pages})", min_value=1, max_value=pages,
                            value=1, step=1)
    start = (page - 1) * page_size
    end = start + page_size
    lo = min(start + 1, total)
    st.caption(f"Showing {lo}–{min(end, total)} of {total} matching task(s) "
               f"(out of {len(df)} total).")
    st.dataframe(view.iloc[start:end], use_container_width=True, hide_index=True)

    st.download_button(
        "⬇️ Download all matches as CSV",
        data=view.to_csv(index=False).encode("utf-8"),
        file_name="curated_tasks.csv",
        mime="text/csv",
    )


def _fmt_date(v):
    return "" if pd.isna(v) else pd.Timestamp(v).date().isoformat()


def page_pivot(df):
    st.header("🧮 Pivot Builder")
    st.caption("Excel-style: drop fields into Rows / Columns / Values, pick how to "
               "summarize, and copy the result as an image.")
    if df.empty:
        st.info("No data to pivot yet."); return

    pool = list(COLUMNS)
    agg_names = list(AGGREGATIONS.keys())

    with st.container(border=True):
        f1, f2, f3 = st.columns(3)
        row_fields = f1.multiselect("▤ Rows (required)", pool,
                                    default=["Status"], key="pv_rows")
        col_fields = f2.multiselect("▥ Columns", [c for c in pool if c not in row_fields],
                                    key="pv_cols")
        value_fields = f3.multiselect("Σ Values", pool, key="pv_values")

        value_specs = []
        if value_fields:
            st.markdown("**Summarize each value by:**")
            acols = st.columns(min(len(value_fields), 4))
            for i, vf in enumerate(value_fields):
                agg = acols[i % len(acols)].selectbox(
                    vf, agg_names, index=agg_names.index("Count"), key=f"pv_agg_{vf}")
                value_specs.append((vf, agg))

        o1, o2 = st.columns(2)
        show_as = o1.radio("Show values as",
                           ["Normal", "% of Grand Total", "% of Row Total",
                            "% of Column Total"], key="pv_show_as")
        chart_type = o2.radio("Chart", ["Bar", "Stacked bar", "Pie", "Hide"],
                              key="pv_chart")

        work = df.copy()
        with st.expander("▽ Filters (optional)"):
            filter_fields = st.multiselect("Filter fields", pool, key="pv_filters")
            for ff in filter_fields:
                opts = sorted(x for x in work[ff].astype(str).unique())
                chosen = st.multiselect(f"{ff} =", opts, default=opts, key=f"pv_fv_{ff}")
                work = work[work[ff].astype(str).isin(chosen)]

    if not row_fields:
        st.info("Add at least one **Rows** field to build the pivot."); return
    if work.empty:
        st.warning("No rows match the current filters."); return
    if not value_specs:
        value_specs = [("Task_ID", "Count")]

    # Helper columns (same field can appear under different aggregations).
    agg_count = {}
    for _, agg in value_specs:
        agg_count[agg] = agg_count.get(agg, 0) + 1

    def friendly(vf, agg):
        return agg if agg_count[agg] == 1 else f"{agg} of {vf}"

    workp = work.copy()
    aggfunc, friendly_map, helpers = {}, {}, []
    for i, (vf, agg) in enumerate(value_specs):
        func, needs_num = AGGREGATIONS[agg]
        helper = f"__v{i}__"
        workp[helper] = (pd.to_numeric(workp[vf], errors="coerce")
                         if needs_num else workp[vf])
        aggfunc[helper] = func
        friendly_map[helper] = friendly(vf, agg)
        helpers.append(helper)

    try:
        pivot = pd.pivot_table(
            workp, index=row_fields, columns=col_fields or None, values=helpers,
            aggfunc=aggfunc, margins=True, margins_name="Grand Total",
            observed=False, fill_value=0,
        )
    except Exception as exc:
        st.error(f"Could not build this pivot: {exc}"); return

    single_value = len(value_specs) == 1

    def flat_col(c):
        if isinstance(c, tuple):
            first = c[0]
            if first in friendly_map:                 # a value column
                rest = [str(p) for p in c[1:] if str(p) not in ("", "nan")]
                if single_value:
                    label = " | ".join(rest)
                else:
                    label = " | ".join([friendly_map[first]] + rest)
                return label if label else "Grand Total"
            return str(first)                          # an index column: ('Status','')
        return friendly_map.get(c, str(c))

    def make_unique(cols):
        seen, out = {}, []
        for c in cols:
            if c in seen:
                seen[c] += 1; out.append(f"{c}.{seen[c]}")
            else:
                seen[c] = 0; out.append(c)
        return out

    flat = pivot.reset_index()
    flat.columns = make_unique([flat_col(c) for c in flat.columns])
    value_cols = [c for c in flat.columns if c not in row_fields]

    # --- Show values as % (single value only) ---------------------------
    apply_pct = show_as != "Normal" and single_value
    if show_as != "Normal" and not single_value:
        st.caption("ℹ️ % modes apply when exactly one Value is selected — showing Normal.")
    if apply_pct:
        try:
            num = flat[value_cols].apply(pd.to_numeric, errors="coerce")
            trow = flat.index[flat[row_fields[0]].astype(str) == "Grand Total"]
            gt_col = "Grand Total" if "Grand Total" in value_cols else None
            if show_as == "% of Grand Total" and gt_col and len(trow):
                denom = num.loc[trow[0], gt_col]
                num = num / denom * 100 if denom else num
            elif show_as == "% of Row Total" and gt_col:
                num = num.div(num[gt_col].replace(0, np.nan), axis=0) * 100
            elif show_as == "% of Column Total" and len(trow):
                num = num.div(num.loc[trow[0]].replace(0, np.nan), axis=1) * 100
            else:
                apply_pct = False
            if apply_pct:
                flat[value_cols] = num
        except Exception:
            apply_pct = False

    def fmt(v, is_label):
        if is_label:
            return "" if pd.isna(v) else str(v)
        n = pd.to_numeric(pd.Series([v]), errors="coerce").iloc[0]
        if pd.isna(n):
            return ""
        if apply_pct:
            return f"{n:,.1f}%"
        return f"{n:,.0f}" if float(n).is_integer() else f"{n:,.2f}"

    disp = flat.copy()
    for c in disp.columns:
        disp[c] = disp[c].map(lambda v, lbl=(c in row_fields): fmt(v, lbl))

    st.caption(f"Pivoting **{len(work):,}** task(s).")
    st.dataframe(disp, hide_index=True, use_container_width=True)

    with st.expander("📸 Copy / download this pivot as an image", expanded=False):
        body = f'<div class="snap-h">Pivot — {", ".join(row_fields)}'
        body += f' × {", ".join(col_fields)}' if col_fields else ""
        body += "</div>" + df_to_html_table(disp, label_cols=row_fields)
        copyable_widget(body, title="pivot", height=560)

    st.download_button("⬇️ Download pivot CSV",
                       data=disp.to_csv(index=False).encode("utf-8"),
                       file_name="pivot.csv", mime="text/csv")

    # --- Chart of the pivot ---------------------------------------------
    if chart_type != "Hide":
        vf0, agg0 = value_specs[0]
        func0, needs0 = AGGREGATIONS[agg0]
        cw = work.copy()
        cw["_v"] = pd.to_numeric(cw[vf0], errors="coerce") if needs0 else cw[vf0]
        try:
            if chart_type == "Pie":
                pie = cw.groupby(row_fields[0])["_v"].agg(func0).reset_index(name="Value")
                fig = px.pie(pie, names=row_fields[0], values="Value", hole=0.35,
                             color_discrete_sequence=PIVOT_PALETTE)
            else:
                gb = [row_fields[0]] + ([col_fields[0]] if col_fields else [])
                cagg = cw.groupby(gb)["_v"].agg(func0).reset_index(name="Value")
                fig = px.bar(cagg, x=row_fields[0], y="Value",
                             color=(col_fields[0] if col_fields else None),
                             barmode=("stack" if chart_type == "Stacked bar" else "group"),
                             color_discrete_sequence=PIVOT_PALETTE)
            st.plotly_chart(fig, use_container_width=True)
        except Exception as exc:
            st.caption(f"Chart unavailable for this combination ({exc}).")


def page_action_items(df, as_of):
    st.header("🎯 Action Items")
    st.caption(f"What needs attention as of **{as_of:%A, %d %b %Y}**. "
               "Change the date in the sidebar to view any day.")
    if df.empty:
        st.info("No tasks yet."); return

    a = prepare_analytics(df)
    open_mask = ~a["Status"].isin(CLOSED_STATUSES)
    dd, cd, cpd = a["Due_Date"].dt.date, a["Created_Date"].dt.date, a["Completed_Date"].dt.date
    horizon = as_of + datetime.timedelta(days=3)

    overdue = a[open_mask & a["Due_Date"].notna() & (dd < as_of)]
    due_today = a[open_mask & (dd == as_of)]
    due_soon = a[open_mask & a["Due_Date"].notna() & (dd > as_of) & (dd <= horizon)]
    created_today = a[cd == as_of]
    completed_today = a[cpd == as_of]

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("🔴 Overdue", len(overdue))
    m2.metric("🟡 Due today", len(due_today))
    m3.metric("🟢 Due ≤3 days", len(due_soon))
    m4.metric("🆕 Created today", len(created_today))
    m5.metric("✅ Completed today", len(completed_today))
    st.divider()

    cols_show = ["Task_ID", "Title", "Assignee", "Priority", "Status", "Due_Date"]

    def section(title, frame, sort_due=True):
        st.subheader(title)
        if frame.empty:
            st.caption("Nothing here 🎉"); return
        f = frame.sort_values("Due_Date") if sort_due else frame.copy()
        f = f[cols_show].copy()
        f["Due_Date"] = f["Due_Date"].map(_fmt_date)
        st.dataframe(f, hide_index=True, use_container_width=True)

    section("🔴 Overdue — act now", overdue)
    section("🟡 Due today", due_today)
    section("🟢 Due within 3 days", due_soon)
    with st.expander("🆕 Created today  /  ✅ Completed today"):
        section("Created today", created_today, sort_due=False)
        section("Completed today", completed_today, sort_due=False)

    st.divider()
    with st.expander("📸 Copy / download today's action list as an image"):
        cards = [
            {"label": "Overdue", "value": len(overdue), "accent": "#dc2626"},
            {"label": "Due today", "value": len(due_today), "accent": "#f59e0b"},
            {"label": "Due ≤3d", "value": len(due_soon), "accent": "#16a34a"},
            {"label": "Created today", "value": len(created_today), "accent": "#2563eb"},
            {"label": "Completed today", "value": len(completed_today), "accent": "#7c3aed"},
        ]
        body = f'<div class="snap-h">Action items — {as_of:%d %b %Y}</div>'
        body += kpi_cards_html(cards)

        def mini(title, frame):
            if frame.empty:
                return (f'<div class="bcard"><div class="btitle">{title}</div>'
                        f'<div style="color:#64748b">None 🎉</div></div>')
            t = frame.sort_values("Due_Date")[cols_show].head(20).copy()
            t["Due_Date"] = t["Due_Date"].map(_fmt_date)
            return (f'<div class="bcard"><div class="btitle">{title}</div>'
                    f'{df_to_html_table(t, label_cols=cols_show)}</div>')

        body += mini("🔴 Overdue", overdue)
        body += mini("🟡 Due today", due_today)
        copyable_widget(body, title="action_items", height=760)


# ------------------------------------------------------------------ #
#  6. MAIN
# ------------------------------------------------------------------ #

def main():
    st.set_page_config(page_title="Task Tracker", page_icon="✅", layout="wide")

    if not check_password():
        st.stop()

    repo = get_repository()

    with st.sidebar:
        st.title("✅ Task Tracker")
        st.caption(f"Storage backend: `{STORAGE_BACKEND}`")
        if STORAGE_BACKEND == "csv":
            st.caption(f"CSV: `{CSV_PATH}`")
        as_of = st.date_input(
            "🗓️ Treat this date as “today”",
            value=datetime.date.today(),
            help="Drives Due-today / Overdue on the Dashboard and the Action "
                 "Items screen. Defaults to the real date.",
        )
        st.divider()
        if st.button("🔄 Refresh"):
            st.rerun()
        if st.button("🚪 Log out"):
            st.session_state["authenticated"] = False
            st.rerun()

    try:
        df = repo.load_all()
    except Exception as exc:  # pragma: no cover
        st.error(f"Could not load data: {exc}")
        st.stop()

    tabs = st.tabs(
        ["📊 Dashboard", "🎯 Action Items", "🧮 Pivot Builder",
         "➕ Add Task", "✏️ Update / Delete", "🔎 Explore Data"]
    )
    with tabs[0]:
        page_dashboard(df, as_of)
    with tabs[1]:
        page_action_items(df, as_of)
    with tabs[2]:
        page_pivot(df)
    with tabs[3]:
        page_add(repo, df)
    with tabs[4]:
        page_update_delete(repo, df)
    with tabs[5]:
        page_explore(df)


if __name__ == "__main__":
    main()