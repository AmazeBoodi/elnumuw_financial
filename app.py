"""
Alnumuw — Food Delivery Financial Dashboard
Streamlit + Pandas + Plotly Express.

Run:  streamlit run app.py
"""

from __future__ import annotations

import hmac

import pandas as pd
import plotly.express as px
import streamlit as st

from data import (
    COMPENSATION_RATE_OVERRIDE,
    PRICING_ENV,
    PRICING_FILE,
    SALES_ENV,
    SALES_FILE,
    apply_filters,
    break_even_curve,
    break_even_value,
    build_item_pl,
    build_order_pl,
    load_data,
    load_delivery_table,
    load_from_drive,
    normalize_provider,
    order_lines,
    read_secret,
    read_setting,
    read_sheet,
    resolve_source,
    validate_delivery_table,
)

# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Alnumuw • Financial Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ═══════════════════════════════════════════════════════════════════════════
# PASSWORD GATE
# Blocks the entire app until the correct password is entered. The expected
# password is read from st.secrets['APP_PASSWORD'] (configure in Streamlit
# Cloud → App settings → Secrets, or in a local .streamlit/secrets.toml file).
# ═══════════════════════════════════════════════════════════════════════════


def _check_password() -> bool:
    """Return True once the user has entered the correct password.

    The comparison uses `hmac.compare_digest`, which takes the same time
    whether the first character is wrong or only the last is. A plain `==`
    short-circuits on the first mismatching byte, and that timing difference is
    measurable over a network — enough to recover a password character by
    character. Both sides are encoded to bytes so a non-ASCII password compares
    safely instead of raising.
    """
    if st.session_state.get("password_correct", False):
        return True

    st.title("🔒 Alnumuw Financial Dashboard")
    st.markdown("Please enter the access password to continue.")
    pw = st.text_input("Password", type="password", key="_pw_input")
    col1, _ = st.columns([1, 5])
    with col1:
        login_clicked = st.button("Log in", type="primary", key="login_btn")
    if login_clicked:
        expected = read_secret("APP_PASSWORD")
        if not expected:
            st.error(
                "⚠️ APP_PASSWORD is not configured.  \n"
                "Create `.streamlit/secrets.toml` in the project folder and add:  \n"
                "`APP_PASSWORD = \"your-password\"`"
            )
            return False
        if hmac.compare_digest(str(pw).encode("utf-8"),
                               str(expected).encode("utf-8")):
            st.session_state["password_correct"] = True
            st.rerun()
        else:
            st.error("❌ Incorrect password. Please try again.")
    return False


if not _check_password():
    st.stop()

# --- Theme / palette --------------------------------------------------------
# We let Streamlit manage the page background naturally, following the viewer's
# light/dark preference. Nothing here hardcodes a background — an earlier version
# forced a dark canvas, which left dark text on a dark page in light mode.


def _active_theme():
    """'light' or 'dark' for the theme actually in effect for this viewer."""
    try:
        return (getattr(st.context.theme, "type", None) or "light").lower()
    except Exception:
        return "light"


IS_DARK = _active_theme() == "dark"
PLOTLY_TEMPLATE = "plotly_dark" if IS_DARK else "plotly_white"
PALETTE = px.colors.qualitative.Vivid

if IS_DARK:
    COLOR_SALES = "#4C9AFF"
    COLOR_COST = "#FF6B6B"
    COLOR_AGG = "#FFD166"
    COLOR_DELIVERY = "#C77DFF"
    COLOR_NET = "#06D6A0"
else:
    # Deeper variants — the dark-mode palette is too pale to read on white.
    COLOR_SALES = "#2563EB"
    COLOR_COST = "#DC2626"
    COLOR_AGG = "#D97706"
    COLOR_DELIVERY = "#7C3AED"
    COLOR_NET = "#059669"

# One font stack everywhere — Segoe UI and Tahoma both carry a full Arabic set,
# so Arabic and Latin text render in the same family rather than falling back to
# different faces mid-table.
FONT_STACK = "'Segoe UI', Tahoma, Arial, 'Helvetica Neue', sans-serif"

# Profit / loss colours, used by the conditional table styling below.
GREEN = "#16A34A"
RED = "#DC2626"

# Card styling uses neutral translucent greys and inherits Streamlit's own text
# colour, so it reads correctly on either background without being told which.
CUSTOM_CSS = f"""
<style>
    /* Left-to-right throughout, including inside the data tables. Stated
       explicitly rather than relying on the default so nothing inherits a
       different direction from the browser or a stray container. */
    [data-testid="stAppViewContainer"], [data-testid="stSidebar"],
    [data-testid="stHeader"], [data-testid="stPlotlyChart"],
    [data-testid="stDataFrame"], [data-testid="stDataFrameResizable"] {{
        direction: ltr;
    }}
    /* Cell contents read left-to-right too. Numeric columns keep the
       right-alignment Streamlit gives them, which is what makes a column of
       figures readable — that is alignment, not direction. */
    [data-testid="stDataFrame"] * {{ direction: ltr; }}

    /* One font family for every element, including inputs and table cells,
       which Streamlit otherwise leaves to the browser default. */
    html, body, [data-testid="stAppViewContainer"], [data-testid="stSidebar"],
    input, textarea, select, button,
    [data-testid="stDataFrame"], [data-testid="stMetricValue"],
    .kpi-card, .kpi-label, .kpi-value, .kpi-sub {{
        font-family: {FONT_STACK} !important;
    }}

    .kpi-card {{
        background: rgba(128, 128, 128, 0.08);
        border: 1px solid rgba(128, 128, 128, 0.25);
        border-radius: 14px;
        padding: 18px 20px; text-align: center; height: 100%;
    }}
    .kpi-label {{ font-size: 0.78rem; letter-spacing: .06em;
                 text-transform: uppercase; margin-bottom: 6px; opacity: .65; }}
    .kpi-value {{ font-size: 1.7rem; font-weight: 700; line-height: 1.1; }}
    .kpi-sub   {{ font-size: 0.72rem; margin-top: 4px; opacity: .55; }}
    div[data-testid="stTabs"] button[data-baseweb="tab"] {{ font-size: 0.95rem; }}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

CURRENCY = "SAR"


def colour_pl(v):
    """Green for profit, red for loss — for numeric and Profit/Loss text alike."""
    if isinstance(v, str):
        if v == "Profit":
            return f"color: {GREEN}; font-weight: 600"
        if v == "Loss":
            return f"color: {RED}; font-weight: 600"
        return ""
    if pd.isna(v):
        return ""
    if v > 0:
        return f"color: {GREEN}; font-weight: 600"
    if v < 0:
        return f"color: {RED}; font-weight: 600"
    return ""


def money(x):
    if pd.isna(x):
        return "—"
    return f"{CURRENCY} {x:,.0f}"


def fmt_pct(x):
    if pd.isna(x):
        return "—"
    return f"{x*100:,.1f}%"


def style_fig(fig, height=380):
    # Transparent canvas + a template matching the active theme, so charts sit
    # on Streamlit's own background in both light and dark mode. Font colour is
    # left to the template rather than pinned to one theme's palette.
    fig.update_layout(
        template=PLOTLY_TEMPLATE,
        height=height,
        margin=dict(l=10, r=10, t=50, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        font=dict(family=FONT_STACK),
    )
    return fig


# ---------------------------------------------------------------------------
# Sidebar — data source & session controls. Rendered before any other sidebar
# widget so they sit at the top of the panel.
# ---------------------------------------------------------------------------
st.sidebar.title("📊 Alnumuw")
st.sidebar.caption("Food Delivery • Financial Dashboard")

with st.sidebar:
    st.markdown("### 🗂️ Data source")
    if st.button("🔄 Refresh data from Drive", use_container_width=True,
                 key="refresh_data_btn"):
        load_from_drive.clear()
        load_data.clear()
        st.rerun()
    with st.expander("⚙️ Upload custom files (override)"):
        sales_upload = st.file_uploader(
            "Sales workbook (Merged Items / Merged Orders)",
            type=["xlsx"], key="_upload_sales",
            help="Optional: overrides the Drive auto-load for this session.",
        )
        pricing_upload = st.file_uploader(
            "Pricing engine (Items / Apps_Config / Delivery_Fees)",
            type=["xlsx"], key="_upload_pricing",
            help="Optional: test a revised costing sheet without re-uploading to Drive.",
        )
    st.markdown("---")
    if st.button("🚪 Log out", use_container_width=True, key="logout_btn"):
        st.session_state["password_correct"] = False
        st.rerun()
    st.markdown("---")

try:
    sales_bytes, sales_src = resolve_source(
        "Sales", "DRIVE_FILE_ID", SALES_FILE, SALES_ENV, sales_upload
    )
    pricing_bytes, pricing_src = resolve_source(
        "Pricing engine", "DRIVE_PRICING_FILE_ID", PRICING_FILE, PRICING_ENV,
        pricing_upload,
    )
    df = load_data(sales_bytes, pricing_bytes)
    VAT_FACTOR = read_setting(pricing_bytes, "VAT factor", 1.15)
except Exception as e:
    st.error(f"❌ Error loading data: {e}")
    st.stop()

st.sidebar.caption(f"{sales_src}  \n{pricing_src}")

# ═══════════════════════════════════════════════════════════════════════════
# FILTERING
# Bidirectional cascading: every filter's option list is narrowed by every
# OTHER filter's selection plus the current date range, so you can never pick a
# combination that yields nothing. The user's own picks are unioned back in so
# a selection never silently vanishes when another filter changes.
# ═══════════════════════════════════════════════════════════════════════════
G_MIN, G_MAX = df["Date"].min().date(), df["Date"].max().date()

# filter key -> column in the item-level frame
_FILTERS = {
    "v_Brand": "Brand",
    "v_Provider": "Provider",
    "v_City": "City",
    "v_Status": "Status",
    "v_Item": "Item",
    "v_CostStatus": "Cost Status",
}
_FILTER_KEYS = list(_FILTERS)


def _cb_clear_filters():
    # Setting each key explicitly is more reliable than pop(): Streamlit reads
    # the value from session_state when the widget is instantiated, so SETTING
    # it guarantees the new state on the next rerun.
    for k in _FILTER_KEYS:
        st.session_state[k] = []


def _cb_reset_date():
    st.session_state["date_range_key"] = (G_MIN, G_MAX)


def _cb_set_range(s, e):
    s, e = max(s, G_MIN), min(e, G_MAX)
    if s > e:                       # preset lies entirely outside the data window
        s, e = G_MIN, G_MAX
    st.session_state["date_range_key"] = (s, e)


def _current_date_window():
    dr = st.session_state.get("date_range_key", None)
    if dr and len(dr) == 2:
        return pd.Timestamp(dr[0]), pd.Timestamp(dr[1])
    return pd.Timestamp(G_MIN), pd.Timestamp(G_MAX)


def get_allowed_options(key):
    """Options for one filter, narrowed by the date window and every other filter."""
    col = _FILTERS[key]
    ds, de = _current_date_window()
    sub = df[(df["Date"] >= ds) & (df["Date"] <= de + pd.Timedelta(days=1))]
    for other_key, other_col in _FILTERS.items():
        if other_key == key:
            continue
        picked = st.session_state.get(other_key, [])
        if picked:
            sub = sub[sub[other_col].isin(picked)]
    allowed = set(sub[col].dropna().unique().tolist())
    # Never drop what the user has already chosen.
    allowed |= set(st.session_state.get(key, []) or [])
    return sorted(allowed)


def _count_active(key):
    return len(st.session_state.get(key, []) or [])


def _expander_label(emoji, name, key):
    n = _count_active(key)
    return f"{emoji} {name}" + (f"  ·  {n} selected" if n else "")


# --- Header + date controls (main page, above the tabs) --------------------
st.title("📊 Alnumuw — Financial Dashboard")

# Seed the widget's state once rather than passing a default alongside a key —
# Streamlit warns about that combination, because the default is silently
# ignored on every rerun after the first.
if "date_range_key" not in st.session_state:
    st.session_state["date_range_key"] = (G_MIN, G_MAX)

_c1, _c2 = st.columns([3, 1])
with _c1:
    date_range = st.date_input(
        "📅 Date Range Window",
        min_value=G_MIN, max_value=G_MAX,
        key="date_range_key",
    )
with _c2:
    st.markdown("<div style='height:1.75rem'></div>", unsafe_allow_html=True)
    st.button("📅 Reset Date", use_container_width=True,
              help="Reset the date range to the full data window",
              on_click=_cb_reset_date, key="reset_date_btn")

if isinstance(date_range, (list, tuple)) and len(date_range) == 1:
    date_range = (date_range[0], date_range[0])

# Quick presets, anchored to the LATEST date in the data rather than today's
# calendar date, so "last 30 days" still works when the upload lags a few days.
_anchor = pd.Timestamp(G_MAX)
_this_month_start = _anchor.replace(day=1).date()
_last_month_end = _anchor.replace(day=1) - pd.Timedelta(days=1)
_last_month_start = _last_month_end.replace(day=1).date()
_presets = [
    ("7D", (_anchor - pd.Timedelta(days=6)).date(), G_MAX, "Last 7 days of data"),
    ("30D", (_anchor - pd.Timedelta(days=29)).date(), G_MAX, "Last 30 days of data"),
    ("This Month", _this_month_start, G_MAX, f"{_anchor.strftime('%B %Y')} to date"),
    ("Last Month", _last_month_start, _last_month_end.date(),
     _last_month_end.strftime("%B %Y")),
    ("All", G_MIN, G_MAX, "Entire data history"),
]
_pc = st.columns(len(_presets) + 4)   # trailing empties keep the buttons compact
for _i, (_label, _s, _e, _help) in enumerate(_presets):
    _pc[_i].button(_label, key=f"preset_{_i}", use_container_width=True,
                   help=_help, on_click=_cb_set_range, args=(_s, _e))

compare_on = st.toggle(
    "🔁 Compare with the previous period",
    key="compare_on",
    help="Compares against the equally-long window immediately before the one "
         "selected, with every other filter held identical.",
)


def previous_window(current):
    """The equally-long window ending the day before `current` starts."""
    start, end = pd.Timestamp(current[0]), pd.Timestamp(current[1])
    span = end - start
    prev_end = start - pd.Timedelta(days=1)
    return (prev_end - span).date(), prev_end.date()


# --- Sidebar filters panel --------------------------------------------------
with st.sidebar:
    st.subheader("🔍 Filters Panel")
    st.button("🧹 Clear Filters", use_container_width=True,
              help="Clear every filter selection (the date range is untouched)",
              on_click=_cb_clear_filters, key="clear_filters_btn")

    # Default the status filter to Completed on first load only, so clearing
    # filters afterwards genuinely clears it.
    if "v_Status" not in st.session_state:
        st.session_state["v_Status"] = (
            ["Completed"] if "Completed" in df["Status"].dropna().unique() else []
        )

    _panel = [
        ("🏷️", "Filter by Brand", "v_Brand"),
        ("🚚", "Filter by Aggregator", "v_Provider"),
        ("🏙️", "Filter by City", "v_City"),
        ("📋", "Filter by Order Status", "v_Status"),
        ("🛒", "Filter by Item", "v_Item"),
        ("💰", "Filter by Cost Status", "v_CostStatus"),
    ]
    for emoji, name, key in _panel:
        with st.expander(_expander_label(emoji, name, key),
                         expanded=_count_active(key) > 0):
            st.multiselect(name, options=get_allowed_options(key), key=key,
                           label_visibility="collapsed")

sel = {k: st.session_state.get(k, []) for k in _FILTER_KEYS}
# "Only costed" is now just the Cost Status filter narrowed to Costed — the
# banners below stay quiet when the user has already excluded uncosted lines.
cost_only = set(sel["v_CostStatus"]) == {"Costed"}

fdf = apply_filters(
    df,
    date_range=date_range,
    brands=sel["v_Brand"],
    providers=sel["v_Provider"],
    cities=sel["v_City"],
    statuses=sel["v_Status"],
    items=sel["v_Item"],
    cost_status=sel["v_CostStatus"],
)

# Order-level P&L is built once here and shared by every tab that needs it, so
# the dashboard has a single definition of order profit.
order_pl_all = build_order_pl(fdf, pricing_bytes, VAT_FACTOR)

# Comparison period — same filters, the equally-long window immediately before.
prev_fdf = pd.DataFrame()
prev_order_pl = pd.DataFrame()
prev_range = None
if compare_on and date_range and len(date_range) == 2:
    prev_range = previous_window(date_range)
    prev_fdf = apply_filters(
        df,
        date_range=prev_range,
        brands=sel["v_Brand"],
        providers=sel["v_Provider"],
        cities=sel["v_City"],
        statuses=sel["v_Status"],
        items=sel["v_Item"],
        cost_status=sel["v_CostStatus"],
    )
    if not prev_fdf.empty:
        prev_order_pl = build_order_pl(prev_fdf, pricing_bytes, VAT_FACTOR)
    st.caption(
        f"Comparing **{date_range[0]:%Y-%m-%d} → {date_range[1]:%Y-%m-%d}** "
        f"against **{prev_range[0]:%Y-%m-%d} → {prev_range[1]:%Y-%m-%d}**"
    )

# --- Data-coverage warning banners -----------------------------------------
# A gap in the delivery tier table reads as free delivery, so check it up front.
_delivery_issues = validate_delivery_table(load_delivery_table(pricing_bytes))
if _delivery_issues:
    st.error(
        "🚨 **The `Delivery_Fees` tier table does not cover every order value.** "
        "Orders falling in a gap are charged **zero** delivery, which overstates "
        "their profit:\n\n- " + "\n- ".join(_delivery_issues),
        icon="🚨",
    )
n_total = len(fdf)
n_nocost = int((~fdf["Has Cost"]).sum())
if n_nocost and not cost_only:
    pct = n_nocost / n_total * 100 if n_total else 0
    # Brands where the gap is near-total have no entry in the pricing engine at
    # all, which is a different problem from a discounted line failing to match.
    nocost_by_brand = (
        fdf.groupby("Brand")["Has Cost"].agg(rows="size", matched="sum")
    )
    missing_brands = nocost_by_brand[
        nocost_by_brand["matched"] / nocost_by_brand["rows"] < 0.5
    ].index.tolist()
    brand_note = (
        f" **{', '.join(missing_brands)}** {'is' if len(missing_brands) == 1 else 'are'} "
        f"largely or wholly absent from the pricing engine, so those items carry no cost."
        if missing_brands
        else ""
    )
    lost_revenue = fdf.loc[~fdf["Has Cost"], "Total Sale"].sum()
    st.warning(
        f"⚠️ **{n_nocost:,} of {n_total:,} line items ({pct:.1f}%) are flagged "
        f"*Unknown WSP*** — no entry in the pricing engine's `Items` sheet, so they "
        f"cannot be costed. That is {money(lost_revenue)} of revenue with no cost "
        f"against it, excluded from every profit figure.{brand_note} Most are bundles "
        f"and offers sold as one product but costed as several; they need a recipe "
        f"mapping before they can be priced. The *Uncosted items* list on the "
        f"Cost & Profitability page ranks them by revenue at stake.",
        icon="⚠️",
    )

# Providers with no Apps_Config row get zero aggregator fees, which silently
# flatters their margin — call that out rather than letting it pass as real.
no_cfg = fdf.loc[~fdf["Has Agg Config"], "Provider"].dropna().unique()
if len(no_cfg):
    n_nocfg = int((~fdf["Has Agg Config"]).sum())
    st.warning(
        f"⚠️ **{', '.join(sorted(no_cfg))}** {'has' if len(no_cfg) == 1 else 'have'} no "
        f"aggregator-fee configuration in `Apps_Config`, so commission, bank fee and "
        f"compensation are counted as **zero** on {n_nocfg:,} line items. "
        f"Net profit and margin for "
        f"{'this provider is' if len(no_cfg) == 1 else 'these providers are'} "
        f"overstated.",
        icon="⚠️",
    )

# ---------------------------------------------------------------------------
# KPI BAR (every page) — responsive to filters
# ---------------------------------------------------------------------------


def delta_note(current, previous, higher_is_better=True):
    """A coloured '▲ 12.3% vs previous' line, or empty when not comparing."""
    if previous in (None, 0) or previous is None or pd.isna(previous):
        return ""
    if current is None or pd.isna(current):
        return ""
    change = (current - previous) / abs(previous)
    good = (change >= 0) == higher_is_better
    arrow = "▲" if change >= 0 else "▼"
    colour = GREEN if good else RED
    return (f'<span style="color:{colour};font-weight:600">'
            f'{arrow} {abs(change)*100:,.1f}%</span> vs previous')


def render_cards(cards):
    """Each card is (label, value, sub). `sub` may contain HTML for a delta."""
    cols = st.columns(len(cards))
    for col, (label, value, sub) in zip(cols, cards):
        col.markdown(
            f"""<div class="kpi-card">
                    <div class="kpi-label">{label}</div>
                    <div class="kpi-value">{value}</div>
                    <div class="kpi-sub">{sub}&nbsp;</div>
                </div>""",
            unsafe_allow_html=True,
        )


def pl_by(orders, dims):
    """
    Aggregate the order-level P&L by any dimension(s).

    Every summary table in the dashboard goes through here, so they all carry
    the same definition: revenue ex-VAT, food cost ex-VAT, platform fees on the
    incl-VAT price, and the per-order delivery fee. Orders never span more than
    one brand or aggregator (verified in the data), so grouping on either is
    exact rather than an allocation.
    """
    if orders.empty:
        return pd.DataFrame()
    o = orders.assign(
        **{"Aggregator Fees": orders["Commission"] + orders["Bank Fee"]
           + orders["Compensation"]}
    )
    out = (
        o.groupby(dims, as_index=False)
        .agg(
            Orders=("Order", "count"),
            Revenue=("RSP Exl. Vat", "sum"),
            Cost=("WSP Exl. Vat", "sum"),
            **{"Aggregator Fees": ("Aggregator Fees", "sum")},
            Delivery=("Delivery Fee (from table)", "sum"),
            **{"Net Profit": ("Current Net Profit", "sum")},
        )
        .sort_values("Revenue", ascending=False)
    )
    out["Margin %"] = (out["Net Profit"] / out["Revenue"]).where(out["Revenue"] != 0)
    out["Avg Net / Order"] = out["Net Profit"] / out["Orders"]
    return out


def _headline(data, orders):
    """(sales, orders, aov, revenue excl VAT, net, margin) for one window."""
    sales = data["Total Sale"].sum() if not data.empty else 0
    n_orders = data["Order ID"].nunique() if not data.empty else 0
    rev_excl = orders["RSP Exl. Vat"].sum() if not orders.empty else 0
    net = orders["Current Net Profit"].sum() if not orders.empty else 0
    return (
        sales, n_orders, sales / n_orders if n_orders else 0,
        rev_excl, net, net / rev_excl if rev_excl else None,
    )


def kpi_bar(data, orders, prev_data=None, prev_orders=None):
    """Headline figures. Profit comes from the order-level P&L, so delivery —
    which Alnumuw pays and the customer does not — is always charged. There is
    no 'before delivery' number anywhere on the dashboard."""
    sales, n_orders, aov, rev_excl, net, margin = _headline(data, orders)

    comparing = prev_data is not None and not prev_data.empty
    if comparing:
        p_sales, p_orders, p_aov, _, p_net, p_margin = _headline(
            prev_data, prev_orders if prev_orders is not None else pd.DataFrame()
        )
    else:
        p_sales = p_orders = p_aov = p_net = p_margin = None

    default_sub = (f"{len(orders):,} costed orders, after delivery"
                   if not orders.empty else "—")
    render_cards([
        ("Total Sales", money(sales),
         delta_note(sales, p_sales) if comparing else "incl VAT"),
        ("Total Orders", f"{n_orders:,}",
         delta_note(n_orders, p_orders) if comparing
         else f"{len(data):,} line items"),
        ("Avg Order Value", money(aov),
         delta_note(aov, p_aov) if comparing else "incl VAT"),
        ("Net Profit", money(net),
         delta_note(net, p_net) if comparing else default_sub),
        ("Net Margin", fmt_pct(margin),
         delta_note(margin, p_margin) if comparing
         else "on revenue excl VAT, after delivery"),
    ])


kpi_bar(fdf, order_pl_all, prev_fdf if compare_on else None, prev_order_pl)

if fdf.empty:
    st.info("No data for the current filter selection. Adjust the filters in the sidebar.")
    st.stop()

# ═══════════════════════════════════════════════════════════════════════════
# REPORTS — every report on this page, as tabs. Filters and KPIs above apply
# to all of them, so switching report never loses your place.
# ═══════════════════════════════════════════════════════════════════════════


def render_order_contents(order_id):
    """Show every line inside one order, plus its fee waterfall.

    Shared by the P&L and Order Explorer tabs so the drill-down behaves
    identically wherever you open it from.
    """
    lines = order_lines(fdf, order_id, VAT_FACTOR)
    if lines.empty:
        st.warning(
            f"No line items found for order **{order_id}** in the current "
            f"filter selection.",
            icon="⚠️",
        )
        return

    hdr = order_pl_all[order_pl_all["Order"].astype(str) == str(order_id)]
    st.markdown(f"#### 🧺 Order `{order_id}` — {len(lines)} line items")
    if not hdr.empty:
        h = hdr.iloc[0]
        st.caption(
            f"{h['Brand']} · {h['Provider']} · {h['Date']:%Y-%m-%d} — "
            f"gross profit {money(h['Gross Profit'])}, then commission "
            f"{money(h['Commission'])}, bank {money(h['Bank Fee'])}, delivery "
            f"{money(h['Delivery Fee (from table)'])} → "
            f"**net {money(h['Current Net Profit'])} ({h['Current P/L'].lower()})**"
        )
    else:
        st.caption(
            "This order has no P&L — it contains an *Unknown WSP* line, shown below."
        )
    st.dataframe(
        lines.style.format({
            "Qty": "{:,.0f}",
            "RSP Inc. Vat": "{:,.2f}", "RSP Exl. Vat": "{:,.2f}",
            "WSP Inc. Vat": "{:,.2f}", "WSP Exl. Vat": "{:,.2f}",
            "Line Sale Inc. Vat": "{:,.2f}", "Line Cost Inc. Vat": "{:,.2f}",
            "Gross Profit": "{:,.2f}", "Gross Profit %": "{:.1%}",
        }).map(colour_pl, subset=["Gross Profit", "Gross Profit %"]),
        use_container_width=True, hide_index=True,
    )


st.markdown("<br>", unsafe_allow_html=True)
TAB_LABELS = [
    "📈 Sales Performance",
    "💰 Cost & Profitability",
    "🧾 Order Profitability",
    "🧮 P&L",
    "📐 Break-even & Baskets",
    "🔍 Order Explorer",
]
(tab_sales, tab_cost, tab_orders, tab_pl, tab_breakeven,
 tab_explorer) = st.tabs(TAB_LABELS)

# ===========================================================================
# SALES PERFORMANCE
# ===========================================================================
with tab_sales:
    # Monthly sales trend
    trend = (
        fdf.assign(Period=fdf["Date"].dt.to_period("M").dt.to_timestamp())
        .groupby("Period", as_index=False)["Total Sale"]
        .sum()
        .sort_values("Period")
    )
    fig = px.line(
        trend, x="Period", y="Total Sale", markers=True,
        title="Monthly Sales Trend", color_discrete_sequence=[COLOR_SALES],
    )
    fig.update_traces(line=dict(width=3))
    fig.update_yaxes(title=f"Sales ({CURRENCY})")
    fig.update_xaxes(title="")
    st.plotly_chart(style_fig(fig), use_container_width=True)
    st.caption("Use the Brand and Aggregator filters in the sidebar to refine this trend.")

    # --- Margin trend -------------------------------------------------------
    # Revenue alone hides whether the business is getting healthier. This is the
    # same net profit as the P&L — after delivery — tracked monthly.
    if not order_pl_all.empty:
        mt = order_pl_all.copy()
        mt["Period"] = pd.to_datetime(mt["Date"]).dt.to_period("M").dt.to_timestamp()
        trend_m = (
            mt.groupby("Period", as_index=False)
            .agg(Revenue=("RSP Exl. Vat", "sum"),
                 Net=("Current Net Profit", "sum"),
                 Orders=("Order", "count"))
            .sort_values("Period")
        )
        trend_m["Margin %"] = trend_m["Net"] / trend_m["Revenue"] * 100
        trend_m["Net / Order"] = trend_m["Net"] / trend_m["Orders"]

        fig_m = px.line(
            trend_m, x="Period", y="Margin %", markers=True,
            title="Monthly Net Margin (after delivery)",
            color_discrete_sequence=[COLOR_NET],
        )
        fig_m.update_traces(line=dict(width=3))
        fig_m.add_hline(y=0, line_dash="dash", line_color=COLOR_COST, opacity=0.6)
        fig_m.update_yaxes(title="Net margin %", ticksuffix="%")
        fig_m.update_xaxes(title="")
        fig_m.add_bar(
            x=trend_m["Period"], y=trend_m["Net / Order"], name="Net / order",
            marker_color=COLOR_SALES, opacity=0.35, yaxis="y2",
        )
        fig_m.update_layout(
            yaxis2=dict(title=f"Net per order ({CURRENCY})", overlaying="y",
                        side="right", showgrid=False),
        )
        st.plotly_chart(style_fig(fig_m, height=400), use_container_width=True)

        first, last = trend_m.iloc[0], trend_m.iloc[-1]
        direction = "improved" if last["Margin %"] > first["Margin %"] else "fallen"
        st.caption(
            f"Margin has **{direction}** from {first['Margin %']:.1f}% in "
            f"{first['Period']:%b %Y} to {last['Margin %']:.1f}% in "
            f"{last['Period']:%b %Y} — {money(first['Net / Order'])} → "
            f"{money(last['Net / Order'])} per order."
        )

    c1, c2 = st.columns(2)
    with c1:
        # Profit here is the P&L number — after delivery — so this chart cannot
        # disagree with the P&L tab.
        agg = pl_by(order_pl_all, "Provider")
        if agg.empty:
            st.info("No costed orders in the current selection.")
        else:
            agg_m = agg.melt(id_vars="Provider", value_vars=["Revenue", "Net Profit"],
                             var_name="Metric", value_name="Amount")
            fig2 = px.bar(
                agg_m, x="Provider", y="Amount", color="Metric", barmode="group",
                title="Revenue vs Net Profit by Aggregator (after delivery)",
                color_discrete_map={"Revenue": COLOR_SALES, "Net Profit": COLOR_NET},
            )
            fig2.update_yaxes(title=f"{CURRENCY} (excl VAT)")
            fig2.update_xaxes(title="")
            st.plotly_chart(style_fig(fig2), use_container_width=True)

    with c2:
        top_items = (
            fdf.groupby("Item", as_index=False)["Total Sale"].sum()
            .sort_values("Total Sale", ascending=False).head(10)
        )
        fig3 = px.bar(
            top_items, x="Total Sale", y="Item", orientation="h",
            title="Top 10 Items by Revenue", color="Total Sale",
            color_continuous_scale="Tealgrn",
        )
        fig3.update_layout(yaxis=dict(autorange="reversed"), coloraxis_showscale=False)
        fig3.update_xaxes(title=f"Revenue ({CURRENCY})")
        fig3.update_yaxes(title="")
        st.plotly_chart(style_fig(fig3), use_container_width=True)

# ===========================================================================
# COST & PROFITABILITY
# ===========================================================================
with tab_cost:
    cost_df = fdf[fdf["Has Cost"]].copy()

    # --- Uncosted items worklist --------------------------------------------
    # The actionable output of the Unknown WSP flag: which products to define a
    # cost for first, ranked by the revenue currently flying blind.
    uncosted = fdf[~fdf["Has Cost"]]
    if not uncosted.empty:
        gaps = (
            uncosted.groupby(["Brand", "Item"], as_index=False)
            .agg(Revenue=("Total Sale", "sum"), Lines=("Item", "count"),
                 Units=("Quantity", "sum"))
            .sort_values("Revenue", ascending=False)
        )
        with st.expander(
            f"🔎 Uncosted items — {len(gaps):,} products, "
            f"{money(gaps['Revenue'].sum())} of revenue with no cost attached"
        ):
            st.caption(
                "Define a WSP for these in the pricing engine's `Items` sheet — or, "
                "for bundles, a recipe mapping them to their component items — and "
                "they flow into every figure automatically. Ranked by revenue at stake."
            )
            st.dataframe(
                gaps.head(200).style.format(
                    {"Revenue": "{:,.0f}", "Lines": "{:,.0f}", "Units": "{:,.0f}"}
                ),
                use_container_width=True, hide_index=True, height=320,
            )
            st.markdown(
                "**Bundles can be costed from their parts.** Add a sheet called "
                "`Recipes` to the pricing engine with columns "
                "**Brand · Bundle · Component · Qty** — one row per component — "
                "and each bundle is costed as the sum of its components' WSP. "
                "The starter file below is pre-filled with every uncosted "
                "product; add a row per component and fill in `Component` and "
                "`Qty`."
            )
            template = gaps[["Brand", "Item"]].rename(columns={"Item": "Bundle"})
            template["Component"] = ""
            template["Qty"] = 1
            st.download_button(
                "⬇️ Download Recipes starter sheet (CSV)",
                data=template.to_csv(index=False).encode("utf-8"),
                file_name="alnumuw_recipes_template.csv", mime="text/csv",
                key="dl_recipe_template",
            )
            st.download_button(
                "⬇️ Export uncosted items as CSV",
                data=gaps.to_csv(index=False).encode("utf-8"),
                file_name="alnumuw_uncosted_items.csv", mime="text/csv",
            )

    # Guarded with if/else rather than st.stop() — inside a tab, st.stop() would
    # abort the whole script and blank every tab after this one.
    if cost_df.empty:
        st.info("No costed items in the current selection, so profitability "
                "charts are unavailable.")
    else:
        # Stacked bar per brand. On the P&L basis this decomposes exactly:
        #   food cost + platform fees + delivery + net = revenue excl VAT
        brand_pl = pl_by(order_pl_all, "Brand")
        if not brand_pl.empty:
            stack = brand_pl.melt(
                id_vars="Brand",
                value_vars=["Cost", "Aggregator Fees", "Delivery", "Net Profit"],
                var_name="Component", value_name="Amount",
            )
            fig = px.bar(
                stack, x="Brand", y="Amount", color="Component",
                title="Revenue Composition by Brand "
                      "(Food + Fees + Delivery + Net = Revenue excl VAT)",
                color_discrete_map={"Cost": COLOR_COST, "Aggregator Fees": COLOR_AGG,
                                    "Delivery": COLOR_DELIVERY, "Net Profit": COLOR_NET},
            )
            fig.update_yaxes(title=f"{CURRENCY} (excl VAT)")
            fig.update_xaxes(title="")
            st.plotly_chart(style_fig(fig, height=420), use_container_width=True)

        # Scatter: item GROSS margin. Deliberately gross, not net — delivery is
        # charged once per order and cannot honestly be split across the items
        # in a basket, so a "net margin per item" would be a made-up number.
        item_scatter = (
            cost_df.groupby(["Brand", "Item"], as_index=False)
            .agg(
                **{"Sales Excl VAT": ("Sale Excl VAT", "sum")},
                **{"Gross Profit": ("Gross Profit", "sum")},
                Volume=("Quantity", "sum"),
                **{"Sale Price": ("Unit Price", "mean")},
            )
        )
        item_scatter["Gross Margin %"] = (
            item_scatter["Gross Profit"] / item_scatter["Sales Excl VAT"]
        ) * 100
        fig2 = px.scatter(
            item_scatter, x="Sale Price", y="Gross Margin %", size="Volume",
            color="Brand", hover_name="Item",
            title="Item Profitability — Sale Price vs Gross Margin (bubble = volume)",
            size_max=45, color_discrete_sequence=PALETTE,
        )
        fig2.add_hline(y=0, line_dash="dash", line_color=COLOR_COST, opacity=0.6)
        fig2.update_xaxes(title=f"Sale Price ({CURRENCY}, incl VAT)")
        st.plotly_chart(style_fig(fig2, height=460), use_container_width=True)
        st.caption(
            "Gross margin, ex-VAT — before platform fees and delivery. Delivery is "
            "charged once per whole order, so splitting it across a basket's items "
            "would invent a number. Net margin lives at order level, on the "
            "**🧮 P&L** and **🧾 Order Profitability** tabs."
        )

        # Table: Brand x Aggregator P&L — same builder as every other summary.
        st.subheader("Brand × Aggregator P&L")
        pivot = pl_by(order_pl_all, ["Brand", "Provider"])
        if pivot.empty:
            st.info("No costed orders in the current selection.")
        else:
            pivot = pivot.sort_values(["Brand", "Revenue"], ascending=[True, False])
            st.dataframe(
                pivot[[
                    "Brand", "Provider", "Orders", "Revenue", "Cost",
                    "Aggregator Fees", "Delivery", "Net Profit", "Margin %",
                ]].style.format({
                    "Orders": "{:,.0f}", "Revenue": "{:,.0f}", "Cost": "{:,.0f}",
                    "Aggregator Fees": "{:,.0f}", "Delivery": "{:,.0f}",
                    "Net Profit": "{:,.0f}", "Margin %": "{:.1%}",
                }).map(colour_pl, subset=["Net Profit", "Margin %"]),
                use_container_width=True, hide_index=True,
            )
            st.caption(
                "Revenue excl VAT, after food cost, platform fees and delivery — "
                "the same definition as the 🧮 P&L tab."
            )

# ===========================================================================
# ORDER PROFITABILITY
# ===========================================================================
with tab_orders:
    # Same builder as the P&L tab, so there is exactly one definition of order
    # profit in the dashboard. This tab analyses it; the P&L tab lists it.
    costed = order_pl_all.copy()
    total_orders = fdf["Order ID"].nunique()

    st.caption(
        "Whole-order economics on the pricing engine's basis: gross profit "
        "ex-VAT, platform fees on the VAT-inclusive price, plus the per-order "
        "delivery fee from the tier table. An order is only costed when every "
        "line in it has a known WSP."
    )

    # Guarded with if/else rather than st.stop() — inside a tab, st.stop() would
    # abort the whole script and blank every tab after this one.
    if costed.empty:
        st.info("No fully-costed orders in the current selection.")
    else:
        costed["Aggregator Fees"] = (
            costed["Commission"] + costed["Bank Fee"] + costed["Compensation"]
        )
        n_excluded = total_orders - len(costed)
        if n_excluded > 0:
            st.warning(
                f"⚠️ **{n_excluded:,} of {total_orders:,} orders "
                f"({n_excluded/total_orders*100:.1f}%) contain at least one "
                f"*Unknown WSP* line** (usually a bundle or offer) and are excluded "
                f"below — a single uncosted line makes the whole order's profit "
                f"unknowable.",
                icon="⚠️",
            )

        # --- Order-level KPI row ---------------------------------------------
        rev = costed["RSP Exl. Vat"].sum()
        net = costed["Current Net Profit"].sum()
        delivery = costed["Delivery Fee (from table)"].sum()
        losing = int((costed["Current Net Profit"] < 0).sum())
        render_cards([
            ("Costed Orders", f"{len(costed):,}", f"of {total_orders:,} in selection"),
            ("Order Revenue", money(rev), "excl VAT"),
            ("Delivery Cost", money(delivery),
             f"{delivery/rev:.1%} of revenue" if rev else ""),
            ("Net Profit", money(net), "after delivery"),
            ("Net Margin", fmt_pct(net / rev if rev else None), "after delivery"),
        ])
        st.divider()

        # --- Where the money goes, per aggregator ----------------------------
        # On the ex-VAT basis this decomposes exactly:
        #   WSP excl + fees + delivery + net = revenue excl VAT
        by_app = pl_by(order_pl_all, "Provider")
        stack = by_app.melt(
            id_vars="Provider",
            value_vars=["Cost", "Aggregator Fees", "Delivery", "Net Profit"],
            var_name="Component", value_name="Amount",
        )
        fig = px.bar(
            stack, x="Provider", y="Amount", color="Component",
            title="Where Every Riyal Goes, by Aggregator (Cost + Fees + Delivery + Net = Revenue)",
            color_discrete_map={
                "Cost": COLOR_COST, "Aggregator Fees": COLOR_AGG,
                "Delivery": COLOR_DELIVERY, "Net Profit": COLOR_NET,
            },
        )
        fig.update_yaxes(title=f"{CURRENCY}")
        fig.update_xaxes(title="")
        st.plotly_chart(style_fig(fig, height=420), use_container_width=True)

        c1, c2 = st.columns(2)
        with c1:
            fig2 = px.histogram(
                costed, x="Current Margin %", nbins=60,
                color_discrete_sequence=[COLOR_SALES],
                title="Distribution of Order Net Margin",
            )
            fig2.add_vline(x=0, line_dash="dash", line_color=COLOR_COST, opacity=0.8)
            fig2.update_xaxes(title="Net margin per order", tickformat=".0%")
            fig2.update_yaxes(title="Orders")
            st.plotly_chart(style_fig(fig2), use_container_width=True)
            st.caption(
                f"**{losing:,} orders ({losing/len(costed):.1%}) lose money** once "
                f"delivery is charged."
            )

        with c2:
            # Break-even by basket size — the single most actionable cut, since
            # delivery is a flat per-order charge small baskets cannot absorb.
            bands = pd.cut(
                costed["RSP Inc. Vat"],
                bins=[0, 30, 40, 50, 60, 80, 100, float("inf")],
                labels=["<30", "30–40", "40–50", "50–60", "60–80", "80–100", "100+"],
                right=False,
            )
            by_band = (
                costed.assign(Band=bands)
                .groupby("Band", as_index=False, observed=True)
                .agg(
                    **{"Net Profit": ("Current Net Profit", "mean")},
                    Orders=("Order", "count"),
                )
            )
            fig3 = px.bar(
                by_band, x="Band", y="Net Profit",
                title="Avg Net Profit per Order, by Basket Size",
                color="Net Profit", color_continuous_scale="RdYlGn",
                color_continuous_midpoint=0,
            )
            fig3.add_hline(y=0, line_dash="dash", line_color=COLOR_COST, opacity=0.6)
            fig3.update_layout(coloraxis_showscale=False)
            fig3.update_xaxes(title=f"Order value ({CURRENCY}, incl VAT)")
            fig3.update_yaxes(title=f"Avg net profit ({CURRENCY})")
            st.plotly_chart(style_fig(fig3), use_container_width=True)
            st.caption("Delivery is a flat charge per order, so small baskets absorb it worst.")

        # --- Aggregator P&L table --------------------------------------------
        st.subheader("Order P&L by Aggregator")
        by_app["Avg Order Value"] = by_app["Revenue"] / by_app["Orders"]
        st.dataframe(
            by_app[[
                "Provider", "Orders", "Revenue", "Cost", "Aggregator Fees", "Delivery",
                "Net Profit", "Avg Order Value", "Avg Net / Order", "Margin %",
            ]].style.format({
                "Orders": "{:,.0f}", "Revenue": "{:,.0f}", "Cost": "{:,.0f}",
                "Aggregator Fees": "{:,.0f}", "Delivery": "{:,.0f}",
                "Net Profit": "{:,.0f}", "Avg Order Value": "{:,.1f}",
                "Avg Net / Order": "{:,.2f}", "Margin %": "{:.1%}",
            }).map(colour_pl, subset=["Net Profit", "Avg Net / Order", "Margin %"]),
            use_container_width=True, hide_index=True,
        )

        st.info(
            "Order-by-order detail — with the full fee waterfall and a "
            "click-through to what was in each order — lives on the **🧮 P&L** "
            "tab, so there is one table rather than two that could drift apart.",
            icon="🧮",
        )

# ===========================================================================
# P&L — the pricing engine's own model, item level and order level
# ===========================================================================


@st.fragment
def _render_pl_tab() -> None:
    """Isolated as a fragment so selecting an order or typing an ID re-runs only
    this tab, not the other four. Filter changes still re-run everything, which
    is correct — they change the data underneath every view."""
    item_pl = build_item_pl(fdf, VAT_FACTOR)
    order_pl = order_pl_all

    st.caption(
        f"Laid out like the pricing engine's own P&L. **Gross profit is measured "
        f"ex-VAT** (VAT collected is not yours to keep) while **platform fees are "
        f"charged on the VAT-inclusive price** — the engine's convention, and "
        f"stricter than the incl-VAT basis the other tabs use, so these figures "
        f"come out lower. VAT factor **{VAT_FACTOR:g}**, read from the engine's "
        f"`README` sheet."
    )

    _MONEY2 = "{:,.2f}"
    _PCT1 = "{:.1%}"

    # --- Item P&L ----------------------------------------------------------
    st.subheader("Item P&L")
    if item_pl.empty:
        st.info("No costed items in the current selection.")
    else:
        st.caption(
            f"{len(item_pl):,} items. Selling price is volume-weighted from what "
            f"actually sold, so discounting pulls the realised price — and the "
            f"margin — down. Sorted by total gross profit."
        )
        st.dataframe(
            item_pl.style.format({
                "RSP Inc. Vat": _MONEY2, "RSP Exl. Vat": _MONEY2,
                "WSP Inc. Vat": _MONEY2, "WSP Exl. Vat": _MONEY2,
                "Gross Profit": _MONEY2, "Gross Profit %": _PCT1,
                "Units Sold": "{:,.0f}", "Total Gross Profit": "{:,.0f}",
            }).map(colour_pl, subset=[
                "Gross Profit", "Gross Profit %", "Total Gross Profit",
            ]),
            use_container_width=True, hide_index=True, height=420,
        )
        st.download_button(
            "⬇️ Export Item P&L as CSV",
            data=item_pl.to_csv(index=False).encode("utf-8"),
            file_name="alnumuw_item_pl.csv", mime="text/csv",
            key="dl_item_pl",
        )

    st.divider()

    # --- Order P&L ---------------------------------------------------------
    st.subheader("Order P&L")
    if order_pl.empty:
        st.info("No fully-costed orders in the current selection.")
    else:
        n_profit = int((order_pl["Current P/L"] == "Profit").sum())
        n_loss = int((order_pl["Current P/L"] == "Loss").sum())
        net_total = order_pl["Current Net Profit"].sum()
        rev_excl = order_pl["RSP Exl. Vat"].sum()
        render_cards([
            ("Orders", f"{len(order_pl):,}", "fully costed"),
            ("Revenue excl VAT", money(rev_excl), ""),
            ("Net Profit", money(net_total), "after all platform costs"),
            ("Margin", fmt_pct(net_total / rev_excl if rev_excl else None), "on excl-VAT revenue"),
            ("Profit / Loss", f"{n_profit:,} / {n_loss:,}", "orders in profit / at a loss"),
        ])
        st.markdown("<br>", unsafe_allow_html=True)

        MAX_ROWS = 2_000
        if len(order_pl) > MAX_ROWS:
            st.caption(
                f"{len(order_pl):,} orders — showing the {MAX_ROWS:,} least "
                f"profitable first. Use the CSV export for the full set."
            )
        else:
            st.caption(f"{len(order_pl):,} orders, least profitable first.")

        st.caption("👆 Click any row to see what was actually in that order.")
        shown = order_pl.head(MAX_ROWS)
        event = st.dataframe(
            shown.style.format({
                "Date": lambda d: d.strftime("%Y-%m-%d") if pd.notna(d) else "",
                "Items": "{:,.0f}", "Units": "{:,.0f}",
                "RSP Inc. Vat": _MONEY2, "RSP Exl. Vat": _MONEY2,
                "WSP Inc. Vat": _MONEY2, "WSP Exl. Vat": _MONEY2,
                "Gross Profit": _MONEY2, "Gross Profit %": _PCT1,
                "Commission %": _PCT1, "Commission": _MONEY2,
                "Bank Fee %": _PCT1, "Bank Fee": _MONEY2,
                "Compensation %": _PCT1, "Compensation": _MONEY2,
                "Delivery Fee (from table)": _MONEY2,
                "Current Net Profit": _MONEY2, "Current Margin %": _PCT1,
            }).map(colour_pl, subset=[
                "Gross Profit", "Gross Profit %",
                "Current Net Profit", "Current Margin %", "Current P/L",
            ]),
            use_container_width=True, hide_index=True, height=460,
            key="order_pl_table", on_select="rerun", selection_mode="single-row",
        )
        st.download_button(
            "⬇️ Export Order P&L as CSV",
            data=order_pl.to_csv(index=False).encode("utf-8"),
            file_name="alnumuw_order_pl_detailed.csv", mime="text/csv",
            key="dl_order_pl",
        )

        # --- Order contents drill-down ---------------------------------------
        picked_rows = list(event.selection.rows) if event and event.selection else []
        lookup = st.text_input(
            "…or look up an order by ID",
            key="order_lookup",
            help="Useful for orders outside the rows shown above, including "
                 "part-costed ones that never reach this table.",
        ).strip()

        chosen = None
        if lookup:
            chosen = lookup
        elif picked_rows:
            chosen = shown.iloc[picked_rows[0]]["Order"]

        if chosen is not None:
            render_order_contents(chosen)


with tab_pl:
    _render_pl_tab()


# ===========================================================================
# BREAK-EVEN & BASKETS
# ===========================================================================
@st.fragment
def _render_breakeven_tab() -> None:
    st.caption(
        "Order-level break-even — **fixed costs excluded**. This is the point "
        "where a single order covers its own food cost, platform fees and "
        "delivery. Delivery is a flat charge per order, so the curve is "
        "sawtoothed: an order can be profitable just under a delivery threshold "
        "and lose money just over it."
    )

    if order_pl_all.empty:
        st.info("No costed orders in the current selection.")
        return

    delivery_tbl = load_delivery_table(pricing_bytes)
    apps_cfg = read_sheet(pricing_bytes, "Apps_Config").dropna(subset=["App"])
    op = order_pl_all.assign(
        _p=order_pl_all["Provider"].map(normalize_provider)
    )

    # --- Break-even per aggregator -----------------------------------------
    rows, curves = [], []
    for _, cfg in apps_cfg.iterrows():
        app_name = cfg["App"]
        sub = op[op["_p"] == app_name]
        if sub.empty:
            continue
        cogs = sub["WSP Inc. Vat"].sum() / sub["RSP Inc. Vat"].sum()
        fee = (float(cfg.get("Commission %", 0) or 0)
               + float(cfg.get("Bank Fee %", 0) or 0)
               + float(COMPENSATION_RATE_OVERRIDE or 0))
        be, dz_lo, dz_hi = break_even_value(
            fee, cogs, delivery_tbl, app_name, VAT_FACTOR
        )
        aov = sub["RSP Inc. Vat"].mean()
        # A missing break-even (no order value up to the scan limit turns a
        # profit) must stay NaN, not None — None crashes numeric formatting and
        # would read as "zero" rather than "never".
        nan = float("nan")
        rows.append({
            "Aggregator": app_name,
            "Orders": len(sub),
            "Platform Fee %": fee,
            "COGS %": cogs,
            "Break-even Order": be if be is not None else nan,
            "Actual AOV": aov,
            "Gap": (aov - be) if be is not None else nan,
            "% Below Break-even": ((sub["RSP Inc. Vat"] < be).mean()
                                   if be else nan),
        })
        curve = break_even_curve(fee, cogs, delivery_tbl, app_name, VAT_FACTOR)
        curve["Aggregator"] = app_name
        curves.append(curve)

    if not rows:
        st.info("No aggregator configuration matches the current selection.")
        return

    def _num(fmt):
        return lambda v: format(v, fmt) if pd.notna(v) else "never"

    be_tbl = pd.DataFrame(rows).sort_values(
        "% Below Break-even", ascending=False, na_position="first"
    )
    st.subheader("Break-even order value, by aggregator")
    st.dataframe(
        be_tbl.style.format({
            "Orders": "{:,.0f}", "Platform Fee %": "{:.1%}", "COGS %": "{:.1%}",
            "Break-even Order": _num(",.0f"), "Actual AOV": "{:,.1f}",
            "Gap": _num("+,.1f"), "% Below Break-even": _num(".1%"),
        }).map(colour_pl, subset=["Gap"]),
        use_container_width=True, hide_index=True,
    )
    worst = be_tbl.iloc[0]
    if pd.notna(worst["% Below Break-even"]):
        st.caption(
            f"**{worst['Aggregator']}** is the worst placed: an order must reach "
            f"{money(worst['Break-even Order'])} to break even, but the average "
            f"is {money(worst['Actual AOV'])} — "
            f"**{worst['% Below Break-even']:.0%} of its orders are below "
            f"break-even.**"
        )

    # --- The curve ----------------------------------------------------------
    all_curves = pd.concat(curves, ignore_index=True)
    fig = px.line(
        all_curves, x="Order Value", y="Net", color="Aggregator",
        title="Net Profit by Order Value (fixed costs excluded)",
        color_discrete_sequence=PALETTE,
    )
    fig.add_hline(y=0, line_dash="dash", line_color=COLOR_COST, opacity=0.8)
    fig.update_xaxes(title=f"Order value ({CURRENCY}, incl VAT)")
    fig.update_yaxes(title=f"Net profit per order ({CURRENCY})")
    st.plotly_chart(style_fig(fig, height=440), use_container_width=True)
    st.caption(
        "Each downward step is a delivery band boundary. Where a line sits "
        "below zero, orders of that size lose money on that aggregator."
    )

    st.divider()

    # --- Minimum order value ------------------------------------------------
    st.subheader("What would a minimum order value do?")
    floor = st.slider(
        f"Minimum order value ({CURRENCY}, incl VAT)",
        min_value=0, max_value=120, value=60, step=5, key="mov_floor",
        help="Orders below this line would no longer be taken. Shows what you "
             "would stop losing — and what revenue you would give up.",
    )
    below = order_pl_all[order_pl_all["RSP Inc. Vat"] < floor]
    above = order_pl_all[order_pl_all["RSP Inc. Vat"] >= floor]
    render_cards([
        ("Orders Below", f"{len(below):,}",
         f"{len(below)/len(order_pl_all):.1%} of orders"),
        ("Their Net Today", money(below["Current Net Profit"].sum()),
         "what you stop earning — or losing"),
        ("Revenue Given Up", money(below["RSP Exl. Vat"].sum()), "excl VAT"),
        ("Net After The Floor", money(above["Current Net Profit"].sum()),
         f"vs {money(order_pl_all['Current Net Profit'].sum())} today"),
        ("Change", money(above["Current Net Profit"].sum()
                         - order_pl_all["Current Net Profit"].sum()),
         "net effect of the floor"),
    ])
    st.caption(
        "This assumes those orders simply do not happen. In practice some "
        "customers top up to reach the floor, so treat this as the cautious end "
        "of the range."
    )

    # --- Where the losses actually sit --------------------------------------
    st.subheader("Where the losses sit")
    losers = order_pl_all[order_pl_all["Current Net Profit"] < 0]
    if losers.empty:
        st.success("No loss-making orders in the current selection.")
    else:
        bands = pd.cut(
            losers["RSP Inc. Vat"],
            bins=[0, 30, 40, 50, 60, 80, 100, float("inf")],
            labels=["<30", "30–40", "40–50", "50–60", "60–80", "80–100", "100+"],
            right=False,
        )
        by_band = (
            losers.assign(Band=bands)
            .groupby("Band", as_index=False, observed=True)
            .agg(Orders=("Order", "count"), Loss=("Current Net Profit", "sum"))
        )
        by_band["Share of Loss"] = by_band["Loss"] / by_band["Loss"].sum()
        fig_b = px.bar(
            by_band, x="Band", y="Loss", title="Total Loss by Basket Size",
            color_discrete_sequence=[COLOR_COST],
        )
        fig_b.update_xaxes(title=f"Order value ({CURRENCY}, incl VAT)")
        fig_b.update_yaxes(title=f"Total loss ({CURRENCY})")
        st.plotly_chart(style_fig(fig_b, height=360), use_container_width=True)
        st.dataframe(
            by_band.style.format({
                "Orders": "{:,.0f}", "Loss": "{:,.0f}",
                "Share of Loss": "{:.0%}",
            }).map(colour_pl, subset=["Loss"]),
            use_container_width=True, hide_index=True,
        )


with tab_breakeven:
    _render_breakeven_tab()


# ===========================================================================
# ORDER EXPLORER
# ===========================================================================
@st.fragment
def _render_explorer_tab() -> None:
    """Fragment: searching and row-selection here re-run only this tab."""
    search = st.text_input("🔎 Search (Order ID, Item, Brand, Provider, City)", "")

    # Line-level view. Profit columns are GROSS (ex-VAT, before platform fees
    # and delivery) because those are per-order, not per-line — net profit lives
    # on the 🧮 P&L tab. Select a row to open the whole order.
    cols = ["Order ID", "Date", "Brand", "Provider", "City", "Item", "Quantity",
            "Total Sale", "Sale Excl VAT", "Total Cost", "Gross Profit",
            "Status", "Cost Status"]
    table = fdf[cols].copy()
    if search:
        s = search.lower()
        mask = (
            table["Order ID"].astype(str).str.contains(s, case=False, na=False)
            | table["Item"].astype(str).str.lower().str.contains(s, na=False)
            | table["Brand"].astype(str).str.lower().str.contains(s, na=False)
            | table["Provider"].astype(str).str.lower().str.contains(s, na=False)
            | table["City"].astype(str).str.lower().str.contains(s, na=False)
        )
        table = table[mask]

    # This is the largest table in the app. It uses `column_config` rather than a
    # pandas Styler: the Styler formats every cell in Python (~0.4s per 2,000
    # rows, paid on every rerun), while column_config formats in the browser and
    # has no cell-count ceiling. The trade-off is no conditional colouring here,
    # which is acceptable — this view is for looking things up, and the coloured
    # P&L tables are a click away.
    MAX_DISPLAY_ROWS = 5_000
    if len(table) > MAX_DISPLAY_ROWS:
        st.caption(
            f"{len(table):,} line items match — showing the first "
            f"{MAX_DISPLAY_ROWS:,}. Narrow the filters or use the CSV export "
            f"below for the full set."
        )
    else:
        st.caption(f"{len(table):,} line items shown")

    st.caption("👆 Click any line to open every item in that order.")
    shown_lines = table.head(MAX_DISPLAY_ROWS)
    money_col = st.column_config.NumberColumn(format="%.2f")
    ev = st.dataframe(
        shown_lines,
        use_container_width=True, hide_index=True, height=480,
        key="explorer_table", on_select="rerun", selection_mode="single-row",
        column_config={
            "Date": st.column_config.DateColumn(format="YYYY-MM-DD"),
            "Quantity": st.column_config.NumberColumn(format="%d"),
            "Total Sale": money_col,
            "Sale Excl VAT": money_col,
            "Total Cost": money_col,
            "Gross Profit": money_col,
        },
    )

    st.download_button(
        "⬇️ Export filtered data as CSV",
        data=table.to_csv(index=False).encode("utf-8"),
        file_name="alnumuw_filtered_orders.csv", mime="text/csv",
    )

    picked = list(ev.selection.rows) if ev and ev.selection else []
    if picked:
        render_order_contents(shown_lines.iloc[picked[0]]["Order ID"])


with tab_explorer:
    _render_explorer_tab()

st.sidebar.divider()
st.sidebar.caption(
    "Cost comes from the pricing engine's `Items` sheet, matched on Brand + item "
    "name; items with no engine entry are flagged **Unknown WSP** and excluded "
    "from profit. Every profit figure is on the engine's basis — gross profit "
    "ex-VAT, platform fees on the incl-VAT price — and **always net of delivery**, "
    "which Alnumuw pays. Line-level views show gross profit only, because "
    "delivery is charged per order and cannot be split across a basket."
)
