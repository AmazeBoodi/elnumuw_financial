"""
Data loading and transformation for the Alnumuw food-delivery dashboard.

Two source workbooks, one job: apply the costing sheet to the sales data.

  Alnumuw-MergedData.xlsx   — sales. Order lines (Merged Items) and order
                              headers (Merged Orders).
  Alnumuw Pricing Engine.xlsx — costing. Item wholesale cost (Items), aggregator
                              fee rates (Apps_Config), tiered delivery fees
                              (Delivery_Fees), global assumptions (README).

Pipeline
--------
1. `resolve_source()`   picks each workbook's bytes: sidebar upload → Google
                        Drive (by file ID in secrets) → local file.
2. `load_data()`        reads **Merged Items** (one row per order line) and
                        joins it to the engine's **Items** sheet via
                        `match_cost()` on brand + item name, then to
                        **Apps_Config** for the aggregator fee rates. The VAT
                        factor comes from the **README** sheet's Setting/Value
                        table. Returns the item-level frame every view builds on.
3. `build_item_pl()`    per-item unit economics.
   `build_order_pl()`   per-order P&L, adding the per-order delivery fee from
                        **Delivery_Fees** (`validate_delivery_table()` checks
                        that table covers every order value).
   `order_lines()`      the lines inside one order, for drill-down.
4. `apply_filters()`    the sidebar selection, applied to the item-level frame.

All business logic lives here so it can be reviewed in one place. The costing
rule is isolated in `match_cost()`; the order roll-up in `build_order_pl()`,
which is the single definition of order profit in the whole dashboard.

Both workbooks are handled as raw bytes rather than file paths, so the same code
serves a local file, a manual upload, or a Google Drive download — and so the
`@st.cache_data` keys are the file contents, meaning a refreshed or re-uploaded
workbook invalidates the cache by itself.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path

import pandas as pd
import streamlit as st

try:  # Streamlit has moved this class between releases.
    from streamlit.errors import StreamlitSecretNotFoundError

    SECRET_MISSING_ERRORS: tuple[type[BaseException], ...] = (
        StreamlitSecretNotFoundError, FileNotFoundError, KeyError,
    )
except ImportError:  # pragma: no cover - older/newer Streamlit
    SECRET_MISSING_ERRORS = (FileNotFoundError, KeyError, Exception)

# --- Default local file names (used when no Drive ID is configured) ---------
BASE_DIR = Path(__file__).parent
SALES_FILE = BASE_DIR / "Alnumuw-MergedData.xlsx"
PRICING_FILE = BASE_DIR / "Alnumuw Pricing Engine.xlsx"

# Environment overrides, so the automated tests can point the app at a
# synthetic dataset without touching secrets or Drive.
SALES_ENV = "ALNUMUW_LOCAL_XLSX"
PRICING_ENV = "ALNUMUW_PRICING_XLSX"

ORDERS_SHEET = "Merged Items"
ITEMS_SHEET = "Items"
APPS_SHEET = "Apps_Config"
DELIVERY_SHEET = "Delivery_Fees"
README_SHEET = "README"
RECIPES_SHEET = "Recipes"

# Parsed sheets are cached to Parquet on disk, keyed by the workbook's content
# hash. Reading `Merged Items` from .xlsx costs ~1.5s; from Parquet ~0.35s, and
# the cache survives a process restart, which st.cache_data does not. A new
# workbook has a new hash, so stale data can never be served.
CACHE_DIR = BASE_DIR / ".cache"
CACHE_MAX_FILES = 40

# Costs (WSP) and sales (Total Amount) are both VAT-inclusive. The item and
# order reports keep that single incl-VAT basis; the P&L report follows the
# pricing engine's own convention instead — see `build_item_pl()`.

UNKNOWN_WSP = "Unknown WSP"
COSTED = "Costed"
COST_SOURCE_DIRECT = "Items sheet"
COST_SOURCE_RECIPE = "Recipe roll-up"

# Both spellings appear in aggregator exports.
CANCELLED_STATUSES = {"Canceled", "Cancelled", "Rejected"}

# Compensation is not actually charged, so it is forced to zero everywhere
# rather than taking the 2% the engine's Apps_Config still carries. Set this to
# None to go back to whatever the sheet says — or, better, zero the column in
# Apps_Config so the engine and the dashboard agree at the source.
COMPENSATION_RATE_OVERRIDE = 0.0


def _compensation(series):
    """Compensation rate per row, honouring the override above."""
    if COMPENSATION_RATE_OVERRIDE is None:
        return series.fillna(0)
    return pd.Series(COMPENSATION_RATE_OVERRIDE, index=series.index, dtype="float64")


# --- Normalization maps -----------------------------------------------------
# Order brand name -> pricing-engine brand name (the engine uses these spellings)
BRAND_MAP = {
    "AlHashimi Sweets": "Alahshimi",
    "Crunchy's": "Crunchys",
    "Cali Pizza Pasta": "cali pizza pasta",
    "Chahrazad’s Bakery": "chahrazad",   # curly apostrophe as it appears in data
    "Chahrazad's Bakery": "chahrazad",         # straight apostrophe fallback
    "Olivia": "Olivia",
}

# Order provider name -> Apps_Config "App" name
PROVIDER_MAP = {
    "Hungerstation": "HungerStation",
    "Toyou": "ToYou",
    "The Chefz": "TheChefz",
    # all other providers stay as-is
}


def normalize_brand(name):
    if pd.isna(name):
        return name
    return BRAND_MAP.get(name, name).strip()


def normalize_provider(name):
    if pd.isna(name):
        return name
    return PROVIDER_MAP.get(name, name)


def item_key(name):
    """
    Join key for an item name: trimmed, lower-cased, and with runs of internal
    whitespace collapsed to one space.

    The collapse matters — `Cheese Kunafa with Blueberry␣␣1/2 kilo` in the sales
    export versus a single space in the engine cost 50 lines their cost match.
    This is deliberately the ONLY fuzziness applied. Anything looser is unsafe
    here: at 88% similarity the nearest match to `Esh El-bolbol Blueberry` is
    `Esh El-bolbol Rasberry` — a different product that would be silently
    mis-costed. Genuine name mismatches belong in the `Items` sheet, not in a
    matching heuristic.
    """
    return re.sub(r"\s+", " ", str(name)).strip().lower()


# ---------------------------------------------------------------------------
# DATA SOURCES
# Data is never committed to the repo. In production both workbooks live on
# Google Drive and are pulled by file ID; locally they sit next to app.py. The
# sidebar's upload override wins over both, for testing new data quickly.
# ---------------------------------------------------------------------------
@st.cache_data(ttl=3600, show_spinner="📥 Downloading latest data from Google Drive…")
def load_from_drive(file_id: str) -> bytes:
    """Download a workbook from Google Drive and return its raw bytes.

    Uses gdown's URL form so this works on every gdown version (older releases
    do not accept the `id=` or `fuzzy=` keyword arguments).
    """
    import gdown

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(tmp_fd)
    try:
        url = f"https://drive.google.com/uc?id={file_id}"
        out = gdown.download(url, output=tmp_path, quiet=True)
        if not out or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            raise RuntimeError(
                "Google Drive returned an empty response. Double-check the file ID "
                "and confirm the file's share setting is 'Anyone with the link can view'."
            )
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def read_sheet(workbook_bytes: bytes, sheet: str, **kwargs) -> pd.DataFrame:
    """Read one sheet, going through an on-disk Parquet cache.

    Excel parsing is by far the slowest step in the whole pipeline — roughly
    1.5s for `Merged Items` versus 0.35s for the same data from Parquet — and
    `st.cache_data` does not survive a process restart, so every app restart
    paid it again. The cache key is a hash of the workbook's *contents* plus the
    sheet name and read options, so a re-uploaded or Drive-refreshed workbook
    misses the cache automatically and stale numbers are impossible.

    Any cache failure (read-only directory, missing pyarrow, corrupt file) falls
    back to reading the workbook directly — the cache is an optimisation, never
    a dependency.
    """
    key = hashlib.sha256(
        workbook_bytes + sheet.encode() + repr(sorted(kwargs.items())).encode()
    ).hexdigest()[:32]
    path = CACHE_DIR / f"{key}.parquet"

    try:
        if path.exists():
            return pd.read_parquet(path)
    except Exception:
        pass  # unreadable cache entry — fall through and re-parse

    frame = pd.read_excel(io.BytesIO(workbook_bytes), sheet_name=sheet, **kwargs)

    try:
        CACHE_DIR.mkdir(exist_ok=True)
        # Column names must be strings for Parquet; blank Excel headers are not.
        frame.columns = [str(c) for c in frame.columns]
        frame.to_parquet(path, index=False)
        _prune_cache()
    except Exception:
        pass  # cache is best-effort

    return frame


def _prune_cache(keep: int = CACHE_MAX_FILES) -> None:
    """Keep the cache bounded by discarding the least recently used entries.

    Every new workbook version mints a new hash, so without this the directory
    grows forever — a few dozen uploads, or a test suite that generates a fresh
    synthetic workbook per run, would quietly fill the disk.
    """
    try:
        files = sorted(CACHE_DIR.glob("*.parquet"), key=lambda p: p.stat().st_mtime)
        for stale in files[:-keep]:
            stale.unlink(missing_ok=True)
    except OSError:
        pass


def read_secret(key: str) -> str | None:
    """Read one Streamlit secret, or None when secrets are unavailable.

    `st.secrets` raises `StreamlitSecretNotFoundError` — not KeyError — when no
    secrets file exists at all, which is the normal case under pytest and in a
    bare local checkout. Catching it here keeps every caller from needing a
    try/except of its own. The specific class is imported defensively because
    its module path has moved between Streamlit versions; the broad fallback is
    the last resort, not the first line of defence.
    """
    try:
        return st.secrets.get(key, None)
    except SECRET_MISSING_ERRORS:
        return None


def resolve_source(label, secret_key, local_path, env_var, uploaded=None):
    """
    Resolve one workbook to bytes, in priority order:
      1. a file uploaded through the sidebar override (this session only)
      2. Google Drive, when its file ID is configured in secrets
      3. a local file next to app.py (path overridable via `env_var`)

    Returns (bytes, description-of-source). Raises FileNotFoundError with an
    actionable message when nothing is available.
    """
    if uploaded is not None:
        return uploaded.read(), f"📎 {label}: manual upload (override active)"

    file_id = read_secret(secret_key)
    if file_id:
        return load_from_drive(file_id), f"☁️ {label}: Google Drive"

    path = os.environ.get(env_var, str(local_path))
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read(), f"📁 {label}: local file ({os.path.basename(path)})"

    raise FileNotFoundError(
        f"{label}: no data source available. Set `{secret_key}` in Streamlit "
        f"secrets, place `{os.path.basename(str(local_path))}` next to app.py, "
        f"or upload a file using the sidebar override."
    )


# ---------------------------------------------------------------------------
@st.cache_data(show_spinner="Loading order & cost data…")
def load_data(sales_bytes: bytes, pricing_bytes: bytes) -> pd.DataFrame:
    """Load, clean, join and enrich the data. Returns the item-level DataFrame.

    Cached on the raw bytes of both workbooks, so replacing either file (via
    Drive refresh or the upload override) recomputes automatically.
    """
    orders = read_sheet(sales_bytes, ORDERS_SHEET)
    cost = read_sheet(pricing_bytes, ITEMS_SHEET)
    apps = read_sheet(pricing_bytes, APPS_SHEET)
    vat_factor = read_setting(pricing_bytes, "VAT factor", 1.15)

    # --- Orders -----------------------------------------------------------
    orders = orders.rename(columns={"Items": "Item", "Total Amount": "Total Sale"})
    orders["Brand_norm"] = orders["Brand"].map(normalize_brand)
    orders["Provider_norm"] = orders["Provider"].map(normalize_provider)
    orders["Date"] = pd.to_datetime(orders["Date"], errors="coerce")

    # Unit (sale) price per item line. Guard against divide-by-zero.
    qty = orders["Quantity"].replace(0, pd.NA)
    orders["Unit Price"] = (orders["Total Sale"] / qty).round(2).astype(float)

    # --- Cost table -------------------------------------------------------
    # The "Items" sheet is the costing master: one row per Brand × Item with its
    # wholesale cost incl VAT. (The "Pricing Engine" sheet repeats the same WSP
    # once per aggregator, so it adds nothing for costing purposes.)
    cost = cost.rename(columns={"Brand": "Brand_norm"})
    cost["Brand_norm"] = cost["Brand_norm"].astype(str).str.strip()
    cost["Item_key"] = cost["Item (English)"].map(item_key)
    cost = cost.dropna(subset=["WSP incl VAT"])

    # Optional bundle recipes, costed from the same Items sheet.
    cost_lookup = cost[["Brand_norm", "Item_key", "WSP incl VAT"]].drop_duplicates(
        subset=["Brand_norm", "Item_key"]
    )
    recipe_costs, _ = build_recipe_costs(load_recipes(pricing_bytes), cost_lookup)

    df = match_cost(orders, cost, recipe_costs)

    # --- Aggregator config join ------------------------------------------
    apps = apps.rename(columns={"App": "Provider_norm"})
    apps_cols = ["Provider_norm", "Commission %", "Bank Fee %", "Compensation %"]
    apps = apps[apps_cols].dropna(subset=["Provider_norm"])
    df = df.merge(apps, on="Provider_norm", how="left")

    # Cancelled lines are costed exactly like completed ones — same WSP, same
    # fees, one uniform process. Which statuses are actually counted is the
    # user's call via the Order Status filter, not a rule buried in here.
    df["Is Cancelled"] = df["Status"].astype(str).str.strip().str.lower().isin(
        {s.lower() for s in CANCELLED_STATUSES}
    )

    # --- Financial calculations -------------------------------------------
    # One basis throughout, matching the pricing engine: gross profit is
    # measured EX-VAT (the VAT collected is remitted, not earned) while platform
    # fees are charged on the VAT-INCLUSIVE price, because that is what the
    # aggregator takes its cut of. See build_item_pl() for the verification.
    df["Sale Excl VAT"] = df["Total Sale"] / vat_factor
    df["Total Cost"] = df["WSP incl VAT"] * df["Quantity"]
    df["Cost Excl VAT"] = df["Total Cost"] / vat_factor

    df["Compensation %"] = _compensation(df["Compensation %"])
    df["Aggregator Commission"] = df["Total Sale"] * df["Commission %"].fillna(0)
    df["Bank Fee"] = df["Total Sale"] * df["Bank Fee %"].fillna(0)
    df["Compensation"] = df["Total Sale"] * df["Compensation %"]
    df["Total Aggregator Cost"] = (
        df["Aggregator Commission"] + df["Bank Fee"] + df["Compensation"]
    )

    df["Has Agg Config"] = df["Commission %"].notna()
    df["Has Cost"] = df["WSP incl VAT"].notna()
    df["Cost Status"] = df["Has Cost"].map({True: COSTED, False: UNKNOWN_WSP})

    df["Gross Profit"] = df["Sale Excl VAT"] - df["Cost Excl VAT"]
    df["Net Profit"] = df["Gross Profit"] - df["Total Aggregator Cost"]
    df["Net Margin %"] = (df["Net Profit"] / df["Sale Excl VAT"]).where(
        df["Sale Excl VAT"] != 0
    )

    return df


@st.cache_data(show_spinner=False)
def load_recipes(pricing_bytes: bytes) -> pd.DataFrame:
    """Read the optional `Recipes` sheet mapping bundles to component items.

    Expected columns: Brand | Bundle | Component | Qty. The sheet does not have
    to exist — without it, bundles simply stay `Unknown WSP`, exactly as before.
    """
    try:
        r = read_sheet(pricing_bytes, RECIPES_SHEET)
    except Exception:
        return pd.DataFrame(columns=["Brand", "Bundle", "Component", "Qty"])

    rename = {c: c.strip().title() for c in r.columns}
    r = r.rename(columns=rename)
    needed = {"Brand", "Bundle", "Component", "Qty"}
    if not needed.issubset(r.columns):
        return pd.DataFrame(columns=["Brand", "Bundle", "Component", "Qty"])

    r = r[list(needed)].dropna(subset=["Brand", "Bundle", "Component"])
    r["Qty"] = pd.to_numeric(r["Qty"], errors="coerce").fillna(1.0)
    return r


def build_recipe_costs(
    recipes: pd.DataFrame, cost_lookup: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Cost each bundle as the sum of its components' WSP × quantity.

    Returns (costed_bundles, incomplete_bundles). A bundle is only costed when
    **every** component resolves to a priced item — a half-priced bundle would
    understate cost and overstate margin, which is worse than leaving it
    flagged. Incomplete ones are returned so the app can name the missing part.
    """
    empty = pd.DataFrame(columns=["Brand_norm", "Item_key", "WSP incl VAT"])
    if recipes.empty:
        return empty, pd.DataFrame(columns=["Brand", "Bundle", "Component"])

    r = recipes.copy()
    r["Brand_norm"] = r["Brand"].map(normalize_brand)
    r["Item_key"] = r["Bundle"].map(item_key)
    r["_component_key"] = r["Component"].map(item_key)

    priced = r.merge(
        cost_lookup.rename(columns={"Item_key": "_component_key"}),
        on=["Brand_norm", "_component_key"], how="left",
    )
    priced["_line_cost"] = priced["WSP incl VAT"] * priced["Qty"]

    complete = (
        priced.groupby(["Brand_norm", "Item_key"])["WSP incl VAT"]
        .apply(lambda s: s.notna().all())
    )
    totals = priced.groupby(["Brand_norm", "Item_key"])["_line_cost"].sum()

    costed = (
        pd.DataFrame({"WSP incl VAT": totals[complete]})
        .reset_index()
    )
    missing = priced[priced["WSP incl VAT"].isna()][
        ["Brand", "Bundle", "Component"]
    ].drop_duplicates()
    return costed, missing


def match_cost(orders, cost, recipe_costs=None):
    """
    Cost-match strategy (BRAND + ITEM NAME).

    Each sales line is matched to the pricing engine's `Items` sheet on
    normalized brand + item name (trimmed, case-insensitive). The engine is the
    single source of truth for cost — nothing is inferred from the sale price,
    so a discounted line still carries its true wholesale cost and simply shows
    a thinner margin, which is what you want when judging promotions.

    Second pass: bundles. A product sold as one line but made of several costed
    items — Combo Pizza, Kunafa Royal Box, Ramadan Exclusive Bundle — has no row
    of its own in `Items`. If the engine has a `Recipes` sheet mapping it to its
    components, `build_recipe_costs()` sums those components and that total is
    used here. Direct matches always win; recipes only fill genuine gaps.

    Lines matching neither are flagged `Unknown WSP` and excluded from all
    profit figures — counted, never guessed at.

    To change the rule, modify only this function.
    """
    cost_lookup = cost[["Brand_norm", "Item_key", "WSP incl VAT"]].drop_duplicates(
        subset=["Brand_norm", "Item_key"]
    )
    orders = orders.copy()
    orders["Item_key"] = orders["Item"].map(item_key)

    merged = orders.merge(cost_lookup, on=["Brand_norm", "Item_key"], how="left")

    if recipe_costs is not None and not recipe_costs.empty:
        merged = merged.merge(
            recipe_costs.rename(columns={"WSP incl VAT": "_recipe_wsp"}),
            on=["Brand_norm", "Item_key"], how="left",
        )
        merged["Cost Source"] = COST_SOURCE_DIRECT
        from_recipe = merged["WSP incl VAT"].isna() & merged["_recipe_wsp"].notna()
        merged.loc[from_recipe, "WSP incl VAT"] = merged.loc[from_recipe, "_recipe_wsp"]
        merged.loc[from_recipe, "Cost Source"] = COST_SOURCE_RECIPE
        merged.loc[merged["WSP incl VAT"].isna(), "Cost Source"] = None
        merged = merged.drop(columns=["_recipe_wsp"])
    else:
        merged["Cost Source"] = merged["WSP incl VAT"].map(
            lambda v: COST_SOURCE_DIRECT if pd.notna(v) else None
        )

    return merged.drop(columns=["Item_key"])


# ---------------------------------------------------------------------------
# P&L REPORT — the pricing engine's own model, reproduced exactly
#
# Verified column by column against the engine's `Pricing Engine` sheet:
#     excl VAT        = incl VAT / VAT factor
#     Gross Profit    = Selling excl VAT − WS excl VAT
#     Gross Profit %  = Gross Profit / Selling excl VAT
#     Commission      = Commission % × Selling **incl** VAT   (likewise Bank,
#                       Compensation) — the platform takes its cut of the gross,
#                       VAT-inclusive amount
#     Net Profit      = Gross Profit − Commission − Bank − Compensation − Delivery
#     Margin %        = Net Profit / Selling excl VAT
#
# Note the deliberate asymmetry: profit is measured ex-VAT (the VAT you collect
# is not yours to keep) while platform fees are charged on the incl-VAT price.
# This is stricter than the incl-VAT basis the other reports use, so P&L figures
# come out lower — that is the engine's model, not a discrepancy.
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def read_setting(pricing_bytes, label, default):
    """Read one global assumption from the engine's README Setting/Value table."""
    try:
        readme = read_sheet(pricing_bytes, README_SHEET, usecols="A:B")
        readme.columns = ["Setting", "Value"]
        row = readme[readme["Setting"].astype(str).str.strip() == label]
        if not row.empty:
            return float(row["Value"].iloc[0])
    except Exception:
        pass
    return default


def _pl_label(net):
    if pd.isna(net):
        return "—"
    if net > 0:
        return "Profit"
    return "Loss" if net < 0 else "Break-even"


@st.cache_data(show_spinner=False)
def build_item_pl(items: pd.DataFrame, vat_factor: float) -> pd.DataFrame:
    """Per-item unit economics: one row per Brand × Item, costed items only.

    Selling price is volume-weighted from what actually sold, so a line that was
    discounted pulls the realised price down and the margin with it.
    """
    costed = items[items["Has Cost"]]
    if costed.empty:
        return pd.DataFrame()

    g = (
        costed.groupby(["Brand", "Item"], as_index=False)
        .agg(
            _sale=("Total Sale", "sum"),
            _units=("Quantity", "sum"),
            _wsp=("WSP incl VAT", "first"),
        )
    )
    g = g[g["_units"] > 0]
    out = pd.DataFrame({
        "Brand": g["Brand"],
        "Item": g["Item"],
        "RSP Inc. Vat": g["_sale"] / g["_units"],
        "WSP Inc. Vat": g["_wsp"],
        "Units Sold": g["_units"],
    })
    out["RSP Exl. Vat"] = out["RSP Inc. Vat"] / vat_factor
    out["WSP Exl. Vat"] = out["WSP Inc. Vat"] / vat_factor
    out["Gross Profit"] = out["RSP Exl. Vat"] - out["WSP Exl. Vat"]
    out["Gross Profit %"] = (out["Gross Profit"] / out["RSP Exl. Vat"]).where(
        out["RSP Exl. Vat"] != 0
    )
    out["Total Gross Profit"] = out["Gross Profit"] * out["Units Sold"]

    return out[[
        "Brand", "Item",
        "RSP Inc. Vat", "RSP Exl. Vat",
        "WSP Inc. Vat", "WSP Exl. Vat",
        "Gross Profit", "Gross Profit %",
        "Units Sold", "Total Gross Profit",
    ]].sort_values("Total Gross Profit", ascending=False)


@st.cache_data(show_spinner=False)
def build_order_pl(
    items: pd.DataFrame, pricing_bytes: bytes, vat_factor: float
) -> pd.DataFrame:
    """Per-order P&L: one row per fully-costed order, full fee waterfall.

    Only fully-costed orders appear — one Unknown WSP line makes the whole
    order's cost, and therefore its profit, unknowable.
    """
    delivery = load_delivery_table(pricing_bytes)

    g = items.groupby("Order ID", as_index=False).agg(
        Date=("Date", "first"),
        Brand=("Brand", "first"),
        Provider=("Provider", "first"),
        Provider_norm=("Provider_norm", "first"),
        _sale=("Total Sale", "sum"),
        _cost=("Total Cost", "sum"),
        _lines=("Item", "count"),
        _units=("Quantity", "sum"),
        _costed=("Has Cost", "sum"),
        _comm=("Commission %", "first"),
        _bank=("Bank Fee %", "first"),
        _comp=("Compensation %", "first"),
    )
    g = g[g["_costed"] == g["_lines"]]
    if g.empty:
        return pd.DataFrame()

    out = pd.DataFrame({
        "Order": g["Order ID"],
        "Date": g["Date"],
        "Brand": g["Brand"],
        "Provider": g["Provider"],
        "Items": g["_lines"],
        "Units": g["_units"],
        "RSP Inc. Vat": g["_sale"],
        "WSP Inc. Vat": g["_cost"],
    })
    out["RSP Exl. Vat"] = out["RSP Inc. Vat"] / vat_factor
    out["WSP Exl. Vat"] = out["WSP Inc. Vat"] / vat_factor
    out["Gross Profit"] = out["RSP Exl. Vat"] - out["WSP Exl. Vat"]
    out["Gross Profit %"] = (out["Gross Profit"] / out["RSP Exl. Vat"]).where(
        out["RSP Exl. Vat"] != 0
    )

    # Platform fees are charged on the VAT-inclusive price.
    incl = out["RSP Inc. Vat"]
    out["Commission %"] = g["_comm"].fillna(0).values
    out["Commission"] = incl * out["Commission %"]
    out["Bank Fee %"] = g["_bank"].fillna(0).values
    out["Bank Fee"] = incl * out["Bank Fee %"]
    out["Compensation %"] = _compensation(g["_comp"]).values
    out["Compensation"] = incl * out["Compensation %"]
    out["Delivery Fee (from table)"] = lookup_delivery_fee(
        g["Provider_norm"].reset_index(drop=True), incl.reset_index(drop=True), delivery
    ).values

    out["Current Net Profit"] = (
        out["Gross Profit"] - out["Commission"] - out["Bank Fee"]
        - out["Compensation"] - out["Delivery Fee (from table)"].fillna(0)
    )
    out["Current Margin %"] = (
        out["Current Net Profit"] / out["RSP Exl. Vat"]
    ).where(out["RSP Exl. Vat"] != 0)
    out["Current P/L"] = out["Current Net Profit"].map(_pl_label)

    return out[[
        "Order", "Date", "Brand", "Provider", "Items", "Units",
        "RSP Inc. Vat", "RSP Exl. Vat",
        "WSP Inc. Vat", "WSP Exl. Vat",
        "Gross Profit", "Gross Profit %",
        "Commission %", "Commission",
        "Bank Fee %", "Bank Fee",
        "Compensation %", "Compensation",
        "Delivery Fee (from table)",
        "Current Net Profit", "Current Margin %", "Current P/L",
    ]].sort_values("Current Net Profit")


def order_lines(items, order_id, vat_factor):
    """
    What is actually inside one order — the line items behind a single row of
    the order P&L, with per-unit and per-line economics.

    Uncosted lines are kept and flagged rather than dropped: when an order is
    excluded from the P&L, this is where you see which product caused it.
    """
    lines = items[items["Order ID"].astype(str) == str(order_id)]
    if lines.empty:
        return pd.DataFrame()

    qty = lines["Quantity"].replace(0, pd.NA)
    out = pd.DataFrame({
        "Item": lines["Item"].values,
        "Qty": lines["Quantity"].values,
        "RSP Inc. Vat": (lines["Total Sale"] / qty).values,
        "WSP Inc. Vat": lines["WSP incl VAT"].values,
        "Line Sale Inc. Vat": lines["Total Sale"].values,
        "Line Cost Inc. Vat": lines["Total Cost"].values,
        "Cost Status": lines["Cost Status"].values,
    })
    out["RSP Exl. Vat"] = out["RSP Inc. Vat"] / vat_factor
    out["WSP Exl. Vat"] = out["WSP Inc. Vat"] / vat_factor
    out["Gross Profit"] = (
        out["Line Sale Inc. Vat"] - out["Line Cost Inc. Vat"]
    ) / vat_factor
    out["Gross Profit %"] = (
        out["Gross Profit"] / (out["Line Sale Inc. Vat"] / vat_factor)
    ).where(out["Line Sale Inc. Vat"] != 0)

    return out[[
        "Item", "Qty",
        "RSP Inc. Vat", "RSP Exl. Vat",
        "WSP Inc. Vat", "WSP Exl. Vat",
        "Line Sale Inc. Vat", "Line Cost Inc. Vat",
        "Gross Profit", "Gross Profit %", "Cost Status",
    ]]


@st.cache_data(show_spinner=False)
def load_delivery_table(pricing_bytes):
    """Tiered per-order delivery fees from the pricing engine."""
    d = read_sheet(pricing_bytes, DELIVERY_SHEET)
    d = d.rename(
        columns={
            "App": "Provider_norm",
            "Min Value (inclusive)": "Min",
            "Max Value (exclusive)": "Max",
            "Delivery Fee": "Fee",
        }
    )
    return d.dropna(subset=["Provider_norm", "Min", "Max", "Fee"])


def break_even_curve(fee_rate, cogs_ratio, delivery, provider, vat_factor,
                     max_value=250.0, step=1.0) -> pd.DataFrame:
    """Net profit as a function of order value, for one aggregator.

    Excludes fixed costs — this is order-level break-even: what an order must be
    worth to cover its own food, platform fees and delivery.

        net(V) = V/vat × (1 − cogs) − V × fee_rate − delivery(V)

    Delivery is a step function of V, so the curve is sawtoothed: it climbs,
    then drops at each band boundary. An order can be profitable at 29 (no
    delivery charged) and lose money at 31 (a flat fee just kicked in).
    """
    values = pd.Series([v * step for v in range(1, int(max_value / step) + 1)])
    deliv = lookup_delivery_fee(
        pd.Series([provider] * len(values)), values, delivery
    ).fillna(0)
    net = values / vat_factor * (1 - cogs_ratio) - values * fee_rate - deliv
    return pd.DataFrame({
        "Order Value": values, "Delivery": deliv.values, "Net": net.values,
    })


def break_even_value(fee_rate, cogs_ratio, delivery, provider, vat_factor,
                     max_value=250.0, step=1.0):
    """Sustained order-level break-even: the value above which an order is
    profitable *and stays* profitable.

    Not the first profitable value — that is trivially tiny, because orders
    under the delivery threshold pay no delivery at all and so "break even" at
    almost nothing. The number that matters operationally is the point past the
    last loss-making value, i.e. where the flat delivery fee is finally covered.

    Returns (break_even, dead_zone_low, dead_zone_high); any may be None.
    """
    if cogs_ratio is None or pd.isna(cogs_ratio):
        return None, None, None

    curve = break_even_curve(fee_rate, cogs_ratio, delivery, provider,
                             vat_factor, max_value, step)
    losing = curve[curve["Net"] <= 0]
    if losing.empty:
        return 0.0, None, None

    last_loss = float(losing["Order Value"].max())
    if last_loss >= max_value - step:
        return None, float(losing["Order Value"].min()), None
    return (last_loss + step,
            float(losing["Order Value"].min()),
            last_loss)


def validate_delivery_table(delivery):
    """
    Check the tier table covers every order value, per aggregator.

    This matters because a gap reads as free delivery: the lookup returns NaN
    and the P&L treats a missing fee as zero cost, quietly inflating profit on
    exactly the orders whose fee nobody defined. Cheaper to shout about the
    hole in the sheet than to hunt the number later.

    Returns a list of human-readable problems; empty means the table is sound.
    """
    issues = []
    for app, bands in delivery.groupby("Provider_norm"):
        b = bands.sort_values("Min")
        if b["Min"].min() > 0:
            issues.append(f"{app}: no band below {b['Min'].min():g}")
        prev = None
        for _, r in b.iterrows():
            if prev is not None:
                if r["Min"] > prev:
                    issues.append(f"{app}: gap between {prev:g} and {r['Min']:g}")
                elif r["Min"] < prev:
                    issues.append(f"{app}: bands overlap at {r['Min']:g}")
            prev = r["Max"]
    return issues


def lookup_delivery_fee(providers, values, delivery):
    """
    Per-order delivery fee: the band where Min <= order value < Max, for that
    aggregator. Providers absent from the table (e.g. Noon) return NA rather
    than 0, so a missing rate is never mistaken for free delivery.
    """
    out = pd.Series(float("nan"), index=providers.index, dtype="float64")
    for provider, bands in delivery.groupby("Provider_norm"):
        rows = providers == provider
        if not rows.any():
            continue
        v = values[rows]
        fee = pd.Series(float("nan"), index=v.index, dtype="float64")
        for _, b in bands.iterrows():
            fee[(v >= b["Min"]) & (v < b["Max"])] = b["Fee"]
        out[rows] = fee
    return out


# --- Filtering helper used by the app --------------------------------------
def apply_filters(
    df: pd.DataFrame,
    date_range: Sequence | None = None,
    brands: Sequence[str] | None = None,
    providers: Sequence[str] | None = None,
    cities: Sequence[str] | None = None,
    statuses: Sequence[str] | None = None,
    items: Sequence[str] | None = None,
    cost_status: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Apply the sidebar selections. An empty selection means 'no constraint'.

    The date window is inclusive of both endpoints for the whole of the end day.
    The upper bound is the last representable instant of that day rather than
    midnight the following morning: `<= end + 1 day` would sweep in an order
    placed at 00:00:00 the next day, which belongs to the next day's numbers.
    """
    out = df
    for values, col in (
        (statuses, "Status"),
        (brands, "Brand"),
        (providers, "Provider"),
        (cities, "City"),
        (items, "Item"),
        (cost_status, "Cost Status"),
    ):
        if values:
            out = out[out[col].isin(values)]

    if date_range and len(date_range) == 2:
        start = pd.to_datetime(date_range[0]).normalize()
        end = (
            pd.to_datetime(date_range[1]).normalize()
            + pd.Timedelta(days=1)
            - pd.Timedelta(nanoseconds=1)
        )
        out = out[(out["Date"] >= start) & (out["Date"] <= end)]
    return out
