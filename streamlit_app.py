"""
SP Pendency Live Dashboard  â  Lifelong Products
Auto-refreshes every 5 minutes from Freshdesk API.

Secrets required (set in Streamlit Cloud > App settings > Secrets):
    FRESHDESK_API_KEY = "your_key_here"
    FRESHDESK_DOMAIN  = "lifelong.freshdesk.com"   # optional, this is the default
"""

import time
from datetime import datetime, timezone

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

# ââ Page config âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
st.set_page_config(
    page_title="SP Pendency | Lifelong",
    page_icon="ð",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ââ Credentials âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
try:
    API_KEY = st.secrets["FRESHDESK_API_KEY"]
    DOMAIN  = st.secrets.get("FRESHDESK_DOMAIN", "lifelong.freshdesk.com")
except Exception:
    st.error("**API key not configured.**  Add `FRESHDESK_API_KEY` to Streamlit Cloud â App settings â Secrets.")
    st.stop()

AUTH         = (API_KEY, "X")
BASE         = f"https://{DOMAIN}/api/v2"
TAT_HOURS    = 72
TARGET_NAMES = {"open", "pending", "reopened", "customer responded", "waiting on customer"}

# ââ Auto-refresh every 5 minutes ââââââââââââââââââââââââââââââââââââââââââââââ
st_autorefresh(interval=5 * 60 * 1000, key="autorefresh")

# ââ Custom CSS ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
st.markdown("""
<style>
  [data-testid="stMetricValue"] { font-size: 2.4rem !important; font-weight: 800 !important; }
  .stTabs [data-baseweb="tab"] { font-size: 14px; font-weight: 600; }
  div[data-testid="metric-container"] {
      background: #f8f9fb;
      border-radius: 10px;
      padding: 14px 18px;
      border-left: 4px solid #667eea;
  }
  .badge-red   { color: #dc2626; font-weight: 700; }
  .badge-green { color: #16a34a; font-weight: 700; }
  .badge-amber { color: #d97706; font-weight: 700; }
</style>
""", unsafe_allow_html=True)


# ââ Data fetching (cached 5 min) ââââââââââââââââââââââââââââââââââââââââââââââ
def _get(endpoint, params=None):
    r = requests.get(f"{BASE}/{endpoint}", auth=AUTH, params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=300, show_spinner=False)
def fetch_data():
    """Pull live SP tickets from Freshdesk, return processed DataFrame + aggregates."""

    # Group ID
    groups   = _get("groups")
    grp      = next((g for g in groups if g["name"] == "Service Partner"), None)
    if not grp:
        raise ValueError("'Service Partner' group not found in Freshdesk.")
    group_id = grp["id"]

    # Status map
    fields   = _get("ticket_fields")
    sf       = next((f for f in fields if f["name"] == "status"), None)
    stat_map = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed", 6: "Customer Responded"}
    if sf:
        for c in sf.get("choices", []):
            stat_map[c["id"]] = c["value"]
    target_ids = [sid for sid, name in stat_map.items() if name.lower() in TARGET_NAMES]

    # Pull tickets
    seen = {}
    for status_id in target_ids:
        for page in range(1, 11):
            batch = _get("tickets", {
                "group_id": group_id, "status": status_id,
                "per_page": 100, "page": page, "include": "tags,stats",
            })
            if not batch:
                break
            for t in batch:
                seen[t["id"]] = t
            if len(batch) < 100:
                break

    # Process
    now  = datetime.now(timezone.utc)
    rows = []
    for t in seen.values():
        cf          = t.get("custom_fields") or {}
        status_name = stat_map.get(t["status"], str(t["status"]))
        partner     = (cf.get("cf_service_partner_name") or "Unknown").strip()
        city        = (cf.get("cf_city") or "").strip().title()
        state       = (cf.get("cf_state") or "").strip().title()
        sp_status   = (cf.get("cf_service_partner_current_status") or "").strip()
        sp_assigned = cf.get("cf_service_partner_assigned_date_stamp") or ""
        erp_model   = (cf.get("cf_erp_model") or cf.get("cf_model") or "Unknown").strip()
        category    = (cf.get("cf_erp_category") or "Unknown").strip()

        hrs = None
        if sp_assigned:
            try:
                dt  = datetime.fromisoformat(sp_assigned.replace("Z", "+00:00"))
                hrs = (now - dt).total_seconds() / 3600
            except Exception:
                pass

        slo     = status_name.lower()
        active  = slo in ("pending", "reopened")
        overdue = active and hrs is not None and hrs > TAT_HOURS
        within  = active and hrs is not None and hrs <= TAT_HOURS

        raw_tags = t.get("tags", [])
        tags = [x["name"] if isinstance(x, dict) else str(x) for x in raw_tags] if isinstance(raw_tags, list) else []
        cant = (sp_status == "Request for SP Reassignment"
                or "Reassigned SP" in tags
                or partner in ("Unknown", ""))

        prio_map = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}
        rows.append({
            "id":        t["id"],
            "subject":   t.get("subject", ""),
            "status":    status_name,
            "priority":  prio_map.get(t.get("priority", 2), "Medium"),
            "partner":   partner,
            "city":      city,
            "state":     state,
            "erp_model": erp_model,
            "category":  category,
            "hrs":       round(hrs, 1) if hrs is not None else None,
            "overdue":   overdue,
            "within":    within,
            "cant":      cant,
            "sp_status": sp_status,
        })

    df = pd.DataFrame(rows)
    fetched_at = datetime.now().strftime("%d %b %Y  %I:%M %p")
    return df, fetched_at


# ââ Aggregation helpers âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def partner_agg(df):
    active = df[df["status"].isin(["Pending", "Reopened"])]
    g = df.groupby("partner").agg(total=("id", "count"), overdue=("overdue", "sum")).reset_index()
    w = active.groupby("partner").apply(
        lambda x: int((x["hrs"] <= TAT_HOURS).sum()) if len(x) else 0
    ).reset_index(name="within")
    p = active.groupby("partner").size().reset_index(name="pending")
    g = g.merge(p, on="partner", how="left").merge(w, on="partner", how="left")
    g["pending"]  = g["pending"].fillna(0).astype(int)
    g["within"]   = g["within"].fillna(0).astype(int)
    g["overdue_pct"] = g.apply(
        lambda r: round(r["overdue"] / r["pending"] * 100, 1) if r["pending"] > 0 else 0, axis=1
    )

    def sev(pct):
        if pct >= 80: return "CRITICAL"
        if pct >= 60: return "HIGH"
        if pct >= 40: return "MEDIUM"
        if pct >= 20: return "WATCH"
        return "OK"

    g["severity"] = g["overdue_pct"].apply(sev)
    return g.sort_values("overdue", ascending=False)


def model_agg(df):
    active = df[df["status"].isin(["Pending", "Reopened"]) & df["hrs"].notna()]
    g = df.groupby(["erp_model", "category"]).agg(
        total=("id", "count"), overdue=("overdue", "sum")
    ).reset_index()
    p = active.groupby(["erp_model", "category"]).size().reset_index(name="pending")
    w = active.groupby(["erp_model", "category"]).apply(
        lambda x: int((x["hrs"] <= TAT_HOURS).sum())
    ).reset_index(name="within")
    avg = active.groupby(["erp_model", "category"])["hrs"].mean().reset_index(name="avg_hrs")

    def buckets(grp):
        h = grp["hrs"]
        return pd.Series({
            "0-24h":   int((h <= 24).sum()),
            "24-48h":  int(((h > 24) & (h <= 48)).sum()),
            "48-72h":  int(((h > 48) & (h <= 72)).sum()),
            "72-120h": int(((h > 72) & (h <= 120)).sum()),
            ">120h":   int((h > 120).sum()),
        })

    bkt = active.groupby(["erp_model", "category"]).apply(buckets).reset_index()
    g = g.merge(p, on=["erp_model", "category"], how="left") \
         .merge(w, on=["erp_model", "category"], how="left") \
         .merge(avg, on=["erp_model", "category"], how="left") \
         .merge(bkt, on=["erp_model", "category"], how="left")
    for c in ["pending", "within", "0-24h", "24-48h", "48-72h", "72-120h", ">120h"]:
        g[c] = g[c].fillna(0).astype(int)
    g["avg_hrs"] = g["avg_hrs"].round(1)
    g["overdue_pct"] = g.apply(
        lambda r: round(r["overdue"] / r["pending"] * 100, 1) if r["pending"] > 0 else 0.0, axis=1
    )
    return g.sort_values("overdue", ascending=False)


# ââ Colour helpers for dataframe styling âââââââââââââââââââââââââââââââââââââ
SEV_COLOR = {"CRITICAL": "#fecaca", "HIGH": "#fed7aa", "MEDIUM": "#fef3c7", "WATCH": "#fef9c3", "OK": "#dcfce7"}

def style_overdue(v):
    return "color:#dc2626;font-weight:700;" if isinstance(v, (int, float)) and v > 0 else ""

def style_sev(v):
    return f"background-color:{SEV_COLOR.get(str(v), '')};font-weight:700;"


# ââ Sidebar âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
with st.sidebar:
    st.image("https://cdn-icons-png.flaticon.com/512/3500/3500833.png", width=48)
    st.title("SP Pendency")
    st.caption("Lifelong Products Â· Service Partner Ops")
    st.divider()

    if st.button("ð  Refresh Now", use_container_width=True, type="primary"):
        st.cache_data.clear()
        st.rerun()

    st.caption("Auto-refresh: every 5 min")
    st.divider()

    st.markdown("**Filters**")
    priority_filter = st.multiselect(
        "Priority", ["Urgent", "High", "Medium", "Low"],
        default=["Urgent", "High", "Medium", "Low"]
    )
    status_filter = st.multiselect(
        "Status", ["Open", "Pending", "Reopened", "Customer Responded"],
        default=["Open", "Pending", "Reopened", "Customer Responded"]
    )
    show_overdue_only = st.checkbox("Overdue only", value=False)
    st.divider()
    st.markdown(f"**TAT threshold:** {TAT_HOURS} hrs")
    st.markdown(f"**Domain:** `{DOMAIN}`")


# ââ Load data âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
with st.spinner("Fetching live data from Freshdeskâ¦"):
    try:
        df_raw, fetched_at = fetch_data()
    except Exception as e:
        st.error(f"Failed to load data: {e}")
        st.stop()

# Apply sidebar filters
df = df_raw.copy()
if priority_filter:
    df = df[df["priority"].isin(priority_filter)]
if status_filter:
    df = df[df["status"].isin(status_filter)]
if show_overdue_only:
    df = df[df["overdue"]]

total      = len(df_raw)
pending    = int(df_raw["status"].isin(["Pending", "Reopened"]).sum())
overdue    = int(df_raw["overdue"].sum())
within_tat = int(df_raw["within"].sum())
cant       = int(df_raw["cant"].sum())


# ââ Header ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
col_h1, col_h2 = st.columns([3, 1])
with col_h1:
    st.markdown("## ð Service Partner Pendency Dashboard")
with col_h2:
    st.markdown(f"<div style='text-align:right;color:#6b7280;font-size:13px;padding-top:12px;'>As of {fetched_at}</div>", unsafe_allow_html=True)

st.divider()

# ââ KPI row âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Total Tickets",      total,      help="All active SP tickets")
k2.metric("Pending / Reopened", pending,    help="Active cases requiring action")
k3.metric("TAT Breached >72h",  overdue,    delta=f"{round(overdue/pending*100,1) if pending else 0}% of pending",
          delta_color="inverse")
k4.metric("Within TAT",         within_tat, delta=f"{round(within_tat/pending*100,1) if pending else 0}% of pending",
          delta_color="normal")
k5.metric("Can't Attend",       cant,       help="SP gave up / reassigned / no partner", delta_color="inverse")

st.markdown("")

# ââ Tabs ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "ð  Overview", "ð¤ Partner Performance", "ð© Model Ageing", "ð Geography", "ð« Tickets"
])


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# TAB 1 â OVERVIEW
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
with tab1:
    c1, c2 = st.columns(2)

    # Status split donut
    with c1:
        st.subheader("Status Split")
        status_counts = df_raw["status"].value_counts().reset_index()
        status_counts.columns = ["Status", "Count"]
        fig = px.pie(status_counts, names="Status", values="Count", hole=0.5,
                     color_discrete_sequence=px.colors.qualitative.Set2)
        fig.update_layout(margin=dict(t=0, b=0, l=0, r=0), height=280, showlegend=True,
                          legend=dict(orientation="h", y=-0.1))
        fig.update_traces(textposition="inside", textinfo="percent+label")
        st.plotly_chart(fig, use_container_width=True)

    # Priority split
    with c2:
        st.subheader("Priority Split")
        prio_counts = df_raw["priority"].value_counts().reset_index()
        prio_counts.columns = ["Priority", "Count"]
        colors = {"Urgent": "#ef4444", "High": "#f59e0b", "Medium": "#667eea", "Low": "#22c55e"}
        fig2 = px.bar(prio_counts, x="Priority", y="Count",
                      color="Priority", color_discrete_map=colors, text="Count")
        fig2.update_layout(margin=dict(t=0, b=0), height=280, showlegend=False,
                           plot_bgcolor="white", yaxis=dict(showgrid=True, gridcolor="#f0f2f8"))
        fig2.update_traces(textposition="outside")
        st.plotly_chart(fig2, use_container_width=True)

    # Overdue vs within TAT by partner (top 12)
    st.subheader("Top 12 Partners â Overdue vs Within TAT")
    pg = partner_agg(df_raw).head(12)
    fig3 = go.Figure()
    fig3.add_bar(name="Overdue", x=pg["partner"], y=pg["overdue"],
                 marker_color="#ef4444", text=pg["overdue"], textposition="inside")
    fig3.add_bar(name="Within TAT", x=pg["partner"], y=pg["within"],
                 marker_color="#22c55e", text=pg["within"], textposition="inside")
    fig3.update_layout(barmode="stack", height=320, margin=dict(t=0, b=80),
                       plot_bgcolor="white", yaxis=dict(showgrid=True, gridcolor="#f0f2f8"),
                       legend=dict(orientation="h", y=1.05),
                       xaxis=dict(tickangle=-30))
    st.plotly_chart(fig3, use_container_width=True)


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# TAB 2 â PARTNER PERFORMANCE
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
with tab2:
    pg_full = partner_agg(df_raw)

    cc1, cc2 = st.columns([2, 1])
    with cc1:
        st.subheader(f"All {len(pg_full)} Partners")
    with cc2:
        sev_filter = st.multiselect("Filter severity",
                                    ["CRITICAL", "HIGH", "MEDIUM", "WATCH", "OK"],
                                    default=["CRITICAL", "HIGH", "MEDIUM", "WATCH", "OK"],
                                    key="sev_filter")

    pg_show = pg_full[pg_full["severity"].isin(sev_filter)] if sev_filter else pg_full
    disp = pg_show[["partner", "total", "pending", "overdue", "within", "overdue_pct", "severity"]].copy()
    disp.columns = ["Partner", "Total", "Pending", "Overdue", "Within TAT", "Overdue %", "Severity"]
    disp["Overdue %"] = disp["Overdue %"].apply(lambda x: f"{x:.1f}%")

    def highlight_row(row):
        sev = row["Severity"]
        bg  = SEV_COLOR.get(sev, "")
        return [f"background-color:{bg}" if sev in ("CRITICAL","HIGH") else "" for _ in row]

    styled = disp.style.apply(highlight_row, axis=1) \
                       .applymap(style_overdue, subset=["Overdue"])
    st.dataframe(styled, use_container_width=True, hide_index=True, height=480)

    # Overdue % bar chart
    st.subheader("Overdue % by Partner")
    pg_chart = pg_full[pg_full["pending"] > 0].copy()
    pg_chart["color"] = pg_chart["overdue_pct"].apply(
        lambda x: "#ef4444" if x >= 60 else ("#f59e0b" if x >= 40 else "#22c55e")
    )
    fig4 = px.bar(pg_chart.sort_values("overdue_pct", ascending=True).tail(25),
                  x="overdue_pct", y="partner", orientation="h",
                  color="overdue_pct",
                  color_continuous_scale=[[0, "#22c55e"], [0.4, "#f59e0b"], [1, "#ef4444"]],
                  text="overdue_pct")
    fig4.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
    fig4.update_layout(height=600, margin=dict(t=0, r=80),
                       plot_bgcolor="white", coloraxis_showscale=False,
                       xaxis=dict(title="Overdue %", showgrid=True, gridcolor="#f0f2f8"),
                       yaxis=dict(title=""))
    st.plotly_chart(fig4, use_container_width=True)


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# TAB 3 â MODEL AGEING
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
with tab3:
    mg = model_agg(df_raw)

    st.subheader(f"ERP Model Ageing â {len(mg)} Models")

    mc1, mc2 = st.columns([3, 1])
    with mc2:
        cat_opts = ["All"] + sorted(df_raw["category"].dropna().unique().tolist())
        cat_sel  = st.selectbox("Category", cat_opts, key="cat_sel")

    mg_show = mg if cat_sel == "All" else mg[mg["category"] == cat_sel]

    disp_m = mg_show[[
        "erp_model", "category", "total", "pending", "overdue", "within",
        "overdue_pct", "avg_hrs", "0-24h", "24-48h", "48-72h", "72-120h", ">120h"
    ]].copy()
    disp_m.columns = [
        "Model", "Category", "Total", "Pending", "Overdue", "Within TAT",
        "Overdue %", "Avg Hrs", "0-24h", "24-48h", "48-72h", "72-120h", ">120h"
    ]
    disp_m["Overdue %"] = disp_m["Overdue %"].apply(lambda x: f"{x:.1f}%")
    disp_m["Avg Hrs"]   = disp_m["Avg Hrs"].apply(lambda x: f"{x:.1f}" if pd.notna(x) else "â")

    def style_model_row(row):
        try:
            pct = float(str(row["Overdue %"]).replace("%", ""))
        except Exception:
            pct = 0
        if pct >= 60:   bg = "#fecaca"
        elif pct >= 40: bg = "#fed7aa"
        else:           bg = ""
        return [f"background-color:{bg}" for _ in row]

    styled_m = disp_m.style.apply(style_model_row, axis=1) \
                            .applymap(style_overdue, subset=["Overdue", ">120h", "72-120h"])
    st.dataframe(styled_m, use_container_width=True, hide_index=True, height=460)

    # Heatmap: model vs ageing bucket
    st.subheader("Ageing Heatmap â Top 20 Models by Overdue")
    top20 = mg_show.sort_values("overdue", ascending=False).head(20)
    heat_data = top20.set_index("erp_model")[["0-24h", "24-48h", "48-72h", "72-120h", ">120h"]]
    fig5 = px.imshow(heat_data, aspect="auto",
                     color_continuous_scale=[[0, "#dcfce7"], [0.3, "#fef3c7"], [0.7, "#fed7aa"], [1, "#fecaca"]],
                     labels=dict(x="Ageing Bucket", y="ERP Model", color="Tickets"))
    fig5.update_layout(height=500, margin=dict(t=20, b=0))
    st.plotly_chart(fig5, use_container_width=True)


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# TAB 4 â GEOGRAPHY
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
with tab4:
    gc1, gc2 = st.columns(2)

    # Top states
    state_g = df_raw.groupby("state").agg(
        total=("id", "count"), overdue=("overdue", "sum")
    ).reset_index().sort_values("overdue", ascending=False).head(15)
    state_g.columns = ["State", "Total", "Overdue"]

    with gc1:
        st.subheader("Top States by Overdue")
        fig6 = px.bar(state_g.sort_values("Overdue"), x="Overdue", y="State", orientation="h",
                      color="Overdue", color_continuous_scale=[[0, "#fef3c7"], [1, "#ef4444"]],
                      text="Overdue")
        fig6.update_traces(textposition="outside")
        fig6.update_layout(height=440, margin=dict(t=0, r=60), coloraxis_showscale=False,
                           plot_bgcolor="white", xaxis=dict(showgrid=True, gridcolor="#f0f2f8"),
                           yaxis=dict(title=""))
        st.plotly_chart(fig6, use_container_width=True)

    # Top cities
    city_g = df_raw.groupby(["city", "state"]).agg(
        total=("id", "count"), overdue=("overdue", "sum")
    ).reset_index().sort_values("overdue", ascending=False).head(15)

    with gc2:
        st.subheader("Top Cities by Overdue")
        fig7 = px.bar(city_g.sort_values("overdue"), x="overdue", y="city", orientation="h",
                      color="overdue", color_continuous_scale=[[0, "#fef3c7"], [1, "#ef4444"]],
                      text="overdue", hover_data=["state"])
        fig7.update_traces(textposition="outside")
        fig7.update_layout(height=440, margin=dict(t=0, r=60), coloraxis_showscale=False,
                           plot_bgcolor="white", xaxis=dict(showgrid=True, gridcolor="#f0f2f8"),
                           yaxis=dict(title=""))
        st.plotly_chart(fig7, use_container_width=True)

    # Summary table
    st.subheader("State Summary")
    state_full = df_raw.groupby("state").agg(
        total=("id", "count"),
        pending=("status", lambda x: x.isin(["Pending", "Reopened"]).sum()),
        overdue=("overdue", "sum"),
    ).reset_index()
    state_full["overdue_pct"] = state_full.apply(
        lambda r: f"{round(r['overdue']/r['pending']*100,1)}%" if r["pending"] > 0 else "â", axis=1
    )
    state_full.columns = ["State", "Total", "Pending", "Overdue", "Overdue %"]
    state_full = state_full.sort_values("Overdue", ascending=False)
    st.dataframe(
        state_full.style.applymap(style_overdue, subset=["Overdue"]),
        use_container_width=True, hide_index=True
    )


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# TAB 5 â TICKETS
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
with tab5:
    tc1, tc2, tc3 = st.columns(3)

    with tc1:
        t_status = st.multiselect("Status", df["status"].unique().tolist(),
                                  default=df["status"].unique().tolist(), key="t_status")
    with tc2:
        t_partner = st.multiselect("Partner (top 10)",
                                   df["partner"].value_counts().head(10).index.tolist(),
                                   key="t_partner")
    with tc3:
        t_tat = st.radio("TAT filter", ["All", "Overdue only", "Within TAT only"],
                         horizontal=True, key="t_tat")

    tdf = df.copy()
    if t_status:
        tdf = tdf[tdf["status"].isin(t_status)]
    if t_partner:
        tdf = tdf[tdf["partner"].isin(t_partner)]
    if t_tat == "Overdue only":
        tdf = tdf[tdf["overdue"]]
    elif t_tat == "Within TAT only":
        tdf = tdf[tdf["within"]]

    st.caption(f"Showing **{len(tdf)}** tickets")

    disp_t = tdf[[
        "id", "subject", "status", "priority", "partner",
        "city", "state", "erp_model", "hrs", "overdue"
    ]].copy()
    disp_t.columns = [
        "Ticket ID", "Subject", "Status", "Priority", "Partner",
        "City", "State", "Model", "Hrs Pending", "Overdue?"
    ]
    disp_t["Overdue?"] = disp_t["Overdue?"].map({True: "YES", False: "NO"})
    disp_t["Hrs Pending"] = disp_t["Hrs Pending"].apply(
        lambda x: f"{x:.1f}" if pd.notna(x) else "â"
    )
    disp_t["Ticket ID"] = disp_t["Ticket ID"].apply(
        lambda x: f"#{x}"
    )

    def style_ticket_row(row):
        if row["Overdue?"] == "YES":
            return ["background-color:#fef2f2"] * len(row)
        return [""] * len(row)

    styled_t = disp_t.style.apply(style_ticket_row, axis=1)
    st.dataframe(styled_t, use_container_width=True, hide_index=True, height=500)

    # Download filtered list
    csv = tdf.to_csv(index=False)
    st.download_button(
        "â¬ Download filtered tickets (CSV)",
        data=csv,
        file_name=f"SP_Tickets_{datetime.now().strftime('%d%b%Y')}.csv",
        mime="text/csv"
    )


# ââ Footer ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
st.divider()
st.caption(
    f"Lifelong Products Â· Service Partner Ops Â· "
    f"TAT policy: >{TAT_HOURS} hrs = breach Â· "
    f"Data source: {DOMAIN} Â· "
    f"Last fetched: {fetched_at}"
)
