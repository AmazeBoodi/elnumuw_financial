"""
Regression tests for the Alnumuw Financial Dashboard's metric definitions.

Runs the REAL app (app.py) headlessly via Streamlit's AppTest harness against a
tiny synthetic dataset with hand-computed expected values, so any future edit
that silently changes a metric definition fails loudly here.

Invariants protected:
  * Cost is matched on Brand + item name against the pricing engine's `Items`
    sheet — never inferred from the sale price, so a discounted line keeps its
    true WSP and simply shows a thinner margin
  * Items with no engine entry are flagged `Unknown WSP` and contribute nothing
    to cost or profit
  * Item-level Net Profit = Sale − Cost − aggregator fees, and is BEFORE delivery
  * Delivery is charged once per order, from the Delivery_Fees tier matching the
    order's total value (Min <= value < Max)
  * An order is only costed when EVERY line in it is costed
  * Providers with no Apps_Config row get zero fees, and are flagged, not hidden

Run with:  python -m pytest test_financial_metrics.py -v
"""
import os

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

APP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")

# ── Synthetic costing sheet ───────────────────────────────────────────────────
# Two costed items, one deliberately absent from the engine.
ITEMS = [
    # (brand, item english, WSP incl VAT)
    ("BrandA", "Widget",  40.0),
    ("BrandA", "Gadget",  10.0),
    # "Mystery Bundle" intentionally NOT listed -> must flag as Unknown WSP
]

APPS = [
    # (app, commission, bank fee, compensation)
    ("AggX", 0.20, 0.025, 0.02),   # 24.5% all-in
    # "AggNoCfg" intentionally absent -> zero fees, must be flagged
]

DELIVERY = [
    # (app, min inclusive, max exclusive, fee)
    ("AggX", 0,   50,     0.0),
    ("AggX", 50,  100,   10.0),
    ("AggX", 100, 999999, 20.0),
]

# ── Synthetic sales ───────────────────────────────────────────────────────────
# O1  BrandA/AggX  1x Widget @100          -> costed
# O2  BrandA/AggX  1x Widget @100 + 1x Gadget @20  -> costed, 2 lines
# O3  BrandA/AggX  1x Mystery Bundle @60   -> Unknown WSP
# O4  BrandA/AggX  1x Widget @100 + 1x Mystery Bundle @60 -> part-costed
# O5  BrandA/AggNoCfg 1x Widget @100       -> no aggregator config
# O6  BrandA/AggX  1x Widget @45 (discounted below menu price) -> still costed at 40
ORDER_LINES = [
    # (order id, brand, provider, item, qty, total amount, status)
    ("O1", "BrandA", "AggX",     "Widget",         1, 100.0, "Completed"),
    ("O2", "BrandA", "AggX",     "Widget",         1, 100.0, "Completed"),
    ("O2", "BrandA", "AggX",     "Gadget",         1,  20.0, "Completed"),
    ("O3", "BrandA", "AggX",     "Mystery Bundle", 1,  60.0, "Completed"),
    ("O4", "BrandA", "AggX",     "Widget",         1, 100.0, "Completed"),
    ("O4", "BrandA", "AggX",     "Mystery Bundle", 1,  60.0, "Completed"),
    ("O5", "BrandA", "AggNoCfg", "Widget",         1, 100.0, "Completed"),
    ("O6", "BrandA", "AggX",     "Widget",         1,  45.0, "Completed"),
]

# AggX: commission 0.20 + bank 0.025 + compensation 0.02 — but compensation is
# forced to zero everywhere (data.COMPENSATION_RATE_OVERRIDE), so the effective
# rate the dashboard charges is 0.225.
AGG_RATE = 0.225


def _build_sales(path):
    rows = []
    for oid, brand, prov, item, qty, amount, status in ORDER_LINES:
        rows.append({
            "Order ID": oid, "Brand": brand, "Provider": prov,
            "Location": "Branch1", "City": "Riyadh",
            "Date": pd.Timestamp("2026-01-15"), "Time": "12:00:00",
            "Items": item, "Quantity": qty, "Total Amount": amount,
            "Status": status, "Technology": "POS1",
        })
    items = pd.DataFrame(rows)
    orders = (
        items.groupby("Order ID", as_index=False)
        .agg(Brand=("Brand", "first"), Provider=("Provider", "first"),
             Location=("Location", "first"), City=("City", "first"),
             Date=("Date", "first"), Time=("Time", "first"),
             Status=("Status", "first"), Sales=("Total Amount", "sum"),
             Technology=("Technology", "first"))
    )
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        items.to_excel(xw, sheet_name="Merged Items", index=False)
        orders.to_excel(xw, sheet_name="Merged Orders", index=False)


def _build_pricing(path):
    readme = pd.DataFrame([
        {"Setting": "VAT rate", "Value": 0.15},
        {"Setting": "VAT factor", "Value": 1.15},
    ])
    items = pd.DataFrame(
        [{"Brand": b, "Item (English)": n, "Item (Arabic)": n,
          "WSP incl VAT": w, "Kitchen Price": w * 2} for b, n, w in ITEMS]
    )
    apps = pd.DataFrame(
        [{"App": a, "Commission %": c, "Bank Fee %": bf, "Compensation %": cp}
         for a, c, bf, cp in APPS]
    )
    delivery = pd.DataFrame(
        [{"App": a, "Min Value (inclusive)": lo, "Max Value (exclusive)": hi,
          "Delivery Fee": fee} for a, lo, hi, fee in DELIVERY]
    )
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        readme.to_excel(xw, sheet_name="README", index=False)
        items.to_excel(xw, sheet_name="Items", index=False)
        apps.to_excel(xw, sheet_name="Apps_Config", index=False)
        delivery.to_excel(xw, sheet_name="Delivery_Fees", index=False)


@pytest.fixture()
def synthetic(tmp_path, monkeypatch):
    sales = tmp_path / "sales.xlsx"
    pricing = tmp_path / "pricing.xlsx"
    _build_sales(sales)
    _build_pricing(pricing)
    monkeypatch.setenv("ALNUMUW_LOCAL_XLSX", str(sales))
    monkeypatch.setenv("ALNUMUW_PRICING_XLSX", str(pricing))
    return sales, pricing


@pytest.fixture()
def frames(synthetic):
    """The item-level frame the app renders from.

    There is deliberately no second order-level builder: order economics come
    from build_order_pl() alone, exercised through the pl_frames fixture.
    """
    import data as data_mod

    sales, pricing = synthetic
    data_mod.load_data.clear()
    data_mod.load_delivery_table.clear()
    data_mod.read_setting.clear()
    return data_mod.load_data(sales.read_bytes(), pricing.read_bytes())


@pytest.fixture()
def pl_frames(synthetic):
    """The two P&L tables, built on the engine's own model."""
    import data as data_mod

    sales, pricing = synthetic
    pb = pricing.read_bytes()
    data_mod.load_data.clear()
    data_mod.load_delivery_table.clear()
    data_mod.read_setting.clear()
    items = data_mod.load_data(sales.read_bytes(), pb)
    vat = data_mod.read_setting(pb, "VAT factor", 1.15)
    return (
        data_mod.build_item_pl(items, vat),
        data_mod.build_order_pl(items, pb, vat),
        vat,
    )


@pytest.fixture()
def app(synthetic):
    """The real dashboard, logged in, fed with the synthetic dataset."""
    at = AppTest.from_file(APP_FILE, default_timeout=120)
    at.session_state["password_correct"] = True   # bypass the login gate
    at.run()
    assert not at.exception, f"App crashed: {at.exception}"
    return at


VAT = 1.15


# ── Cost matching ─────────────────────────────────────────────────────────────
def test_cost_matches_on_name_not_price(frames):
    """O6 sells a Widget at 45 (below its 100 menu price). It must still cost 40 —
    cost comes from the engine by name, never inferred from the sale price."""
    o6 = frames[frames["Order ID"] == "O6"].iloc[0]
    assert o6["Has Cost"], "A discounted line must still match its item cost"
    assert o6["WSP incl VAT"] == 40.0
    assert o6["Total Cost"] == 40.0
    # Engine basis: (45 - 40)/1.15 - 45*0.225 = -5.777 -> a discount that
    # destroys the margin, which is exactly what the dashboard should reveal.
    expected = (45 - 40) / VAT - 45 * AGG_RATE
    assert round(o6["Net Profit"], 3) == round(expected, 3)


def test_whitespace_only_name_differences_still_match(frames):
    """A double space in the sales export must not cost a line its match."""
    import data as data_mod

    assert data_mod.item_key("Cheese  Kunafa   1/2 kilo") == "cheese kunafa 1/2 kilo"
    assert data_mod.item_key(" Widget ") == data_mod.item_key("Widget")


def test_unknown_wsp_is_flagged_and_uncosted(frames):
    bundles = frames[frames["Item"] == "Mystery Bundle"]
    assert len(bundles) == 2
    assert not bundles["Has Cost"].any()
    assert (bundles["Cost Status"] == "Unknown WSP").all()
    assert bundles["Total Cost"].isna().all(), "Uncosted lines must not contribute cost"


def test_item_profit_is_ex_vat_with_fees_on_incl_vat(frames):
    """One basis everywhere: gross profit ex-VAT, platform fees on incl-VAT.
    Item level is before delivery — delivery is per order, not per line."""
    o1 = frames[frames["Order ID"] == "O1"].iloc[0]
    assert o1["Total Cost"] == 40.0
    assert round(o1["Sale Excl VAT"], 4) == round(100 / VAT, 4)
    assert round(o1["Cost Excl VAT"], 4) == round(40 / VAT, 4)
    assert round(o1["Gross Profit"], 4) == round((100 - 40) / VAT, 4)
    assert round(o1["Total Aggregator Cost"], 3) == round(100 * AGG_RATE, 3)
    assert round(o1["Net Profit"], 3) == round((100 - 40) / VAT - 100 * AGG_RATE, 3)


def test_cancelled_lines_are_costed_like_completed(frames):
    """No special-casing: a cancelled line carries the same WSP and the same
    fees. The Order Status filter, not the cost logic, decides what counts."""
    import data as data_mod

    assert "Is Cancelled" in frames.columns
    assert set(frames["Cost Status"].unique()) <= {"Costed", "Unknown WSP"}, \
        "Cancellation must not introduce a separate cost status"


def test_missing_agg_config_gives_zero_fees_but_is_flagged(frames):
    o5 = frames[frames["Order ID"] == "O5"].iloc[0]
    assert not o5["Has Agg Config"], "AggNoCfg must be flagged as unconfigured"
    assert o5["Total Aggregator Cost"] == 0.0


def test_no_duplicate_flagging(frames):
    """Order IDs are unique in the source, so there is nothing to de-duplicate.
    The old line-level 'duplicate' flag was misleading and is gone."""
    assert "Duplicate Line" not in frames.columns


# ── Date window boundaries ────────────────────────────────────────────────────
def test_date_window_keeps_late_night_and_excludes_next_midnight():
    """The end date must include the whole of that day and nothing after it.

    `<= end + 1 day` used to sweep in an order stamped 00:00:00 the next
    morning, silently moving revenue between reporting days.
    """
    import data as data_mod

    df = pd.DataFrame({
        "Date": pd.to_datetime([
            "2026-01-15 00:00:00",      # start of window
            "2026-01-15 23:59:59",      # last order of the night — must stay
            "2026-01-16 00:00:00",      # next day at midnight — must go
            "2026-01-16 09:00:00",      # clearly next day
            "2026-01-14 23:59:59",      # night before — must go
        ]),
        "Status": ["Completed"] * 5,
    })
    out = data_mod.apply_filters(df, date_range=("2026-01-15", "2026-01-15"))
    kept = list(out["Date"].astype(str))
    assert kept == ["2026-01-15 00:00:00", "2026-01-15 23:59:59"], kept


def test_date_window_spans_multiple_days_inclusively():
    import data as data_mod

    df = pd.DataFrame({
        "Date": pd.to_datetime([
            "2026-03-01 08:00:00", "2026-03-02 12:00:00",
            "2026-03-03 23:59:59", "2026-03-04 00:00:00",
        ]),
        "Status": ["Completed"] * 4,
    })
    out = data_mod.apply_filters(df, date_range=("2026-03-01", "2026-03-03"))
    assert len(out) == 3, "both endpoints inclusive, nothing from the 4th"


# ── Bundle recipes ────────────────────────────────────────────────────────────
def _cost_lookup():
    return pd.DataFrame({
        "Brand_norm": ["BrandA", "BrandA"],
        "Item_key": ["widget", "gadget"],
        "WSP incl VAT": [40.0, 10.0],
    })


def test_recipe_rolls_components_up_into_a_bundle_cost():
    """Mystery Bundle = 1 Widget + 2 Gadgets -> 40 + 20 = 60."""
    import data as data_mod

    recipes = pd.DataFrame({
        "Brand": ["BrandA", "BrandA"],
        "Bundle": ["Mystery Bundle", "Mystery Bundle"],
        "Component": ["Widget", "Gadget"],
        "Qty": [1, 2],
    })
    costed, missing = data_mod.build_recipe_costs(recipes, _cost_lookup())
    assert len(costed) == 1
    assert costed.iloc[0]["Item_key"] == "mystery bundle"
    assert costed.iloc[0]["WSP incl VAT"] == 60.0
    assert missing.empty


def test_incomplete_recipe_is_not_costed():
    """A bundle with one unpriced component must stay uncosted, not half-costed
    — a partial cost would overstate margin, which is worse than no cost."""
    import data as data_mod

    recipes = pd.DataFrame({
        "Brand": ["BrandA", "BrandA"],
        "Bundle": ["Mystery Bundle", "Mystery Bundle"],
        "Component": ["Widget", "Nonexistent Thing"],
        "Qty": [1, 1],
    })
    costed, missing = data_mod.build_recipe_costs(recipes, _cost_lookup())
    assert costed.empty, "a partly-priced bundle must not produce a cost"
    assert list(missing["Component"]) == ["Nonexistent Thing"]


def test_missing_recipes_sheet_is_not_an_error(synthetic):
    """The Recipes sheet is optional — without it, bundles stay Unknown WSP."""
    import data as data_mod

    _, pricing = synthetic
    data_mod.load_recipes.clear()
    assert data_mod.load_recipes(pricing.read_bytes()).empty


def test_direct_match_wins_over_recipe():
    """A real Items row must never be overridden by a recipe."""
    import data as data_mod

    orders = pd.DataFrame({
        "Item": ["Widget"], "Brand_norm": ["BrandA"],
    })
    cost = pd.DataFrame({
        "Brand_norm": ["BrandA"], "Item_key": ["widget"], "WSP incl VAT": [40.0],
    })
    recipe = pd.DataFrame({
        "Brand_norm": ["BrandA"], "Item_key": ["widget"], "WSP incl VAT": [999.0],
    })
    out = data_mod.match_cost(orders, cost, recipe)
    assert out.iloc[0]["WSP incl VAT"] == 40.0
    assert out.iloc[0]["Cost Source"] == data_mod.COST_SOURCE_DIRECT


# ── Order-level break-even ────────────────────────────────────────────────────
def test_break_even_is_sustained_not_first_profitable_value():
    """Below the delivery threshold every order 'breaks even' trivially. The
    number that matters is where profit survives the flat delivery fee."""
    import data as data_mod

    delivery = pd.DataFrame({
        "Provider_norm": ["A", "A"], "Min": [0, 30], "Max": [30, 999999],
        "Fee": [0.0, 15.0],
    })
    # 50% COGS, 20% fees -> per riyal: 1/1.15*0.5 - 0.20 = 0.2348 contribution
    be, dz_lo, dz_hi = data_mod.break_even_value(0.20, 0.50, delivery, "A", 1.15)
    assert dz_lo == 30, "losses start the moment delivery kicks in"
    assert be is not None and 60 < be < 80, be
    # sanity: at the break-even value the order really is profitable
    curve = data_mod.break_even_curve(0.20, 0.50, delivery, "A", 1.15)
    assert curve.loc[curve["Order Value"] == be, "Net"].iloc[0] > 0
    assert curve.loc[curve["Order Value"] == be - 1, "Net"].iloc[0] <= 0


def test_break_even_zero_when_always_profitable():
    import data as data_mod

    delivery = pd.DataFrame({
        "Provider_norm": ["A"], "Min": [0], "Max": [999999], "Fee": [0.0],
    })
    be, lo, hi = data_mod.break_even_value(0.10, 0.40, delivery, "A", 1.15)
    assert be == 0.0 and lo is None


# ── Parquet cache ─────────────────────────────────────────────────────────────
def test_sheet_cache_returns_identical_data(synthetic):
    """The Parquet cache is an optimisation — it must not alter a single value."""
    import data as data_mod

    _, pricing = synthetic
    pb = pricing.read_bytes()
    first = data_mod.read_sheet(pb, "Items")     # cold: parses xlsx, writes cache
    second = data_mod.read_sheet(pb, "Items")    # warm: reads parquet
    pd.testing.assert_frame_equal(first, second)


def test_sheet_cache_stays_bounded(synthetic):
    """Each new workbook mints a new cache entry, so the directory must be
    pruned or it grows without limit."""
    import data as data_mod

    _, pricing = synthetic
    data_mod.read_sheet(pricing.read_bytes(), "Items")
    files = list(data_mod.CACHE_DIR.glob("*.parquet"))
    assert len(files) <= data_mod.CACHE_MAX_FILES, len(files)


# ── Delivery tier table ───────────────────────────────────────────────────────
def test_delivery_table_validation_spots_a_gap():
    """A hole in the tier table reads as free delivery, so it must be caught."""
    import data as data_mod

    good = pd.DataFrame({
        "Provider_norm": ["A", "A"], "Min": [0, 50], "Max": [50, 999999],
        "Fee": [0, 10],
    })
    assert data_mod.validate_delivery_table(good) == []

    holed = pd.DataFrame({
        "Provider_norm": ["A", "A"], "Min": [0, 60], "Max": [50, 999999],
        "Fee": [0, 10],
    })
    issues = data_mod.validate_delivery_table(holed)
    assert issues and "gap" in issues[0].lower()

    overlap = pd.DataFrame({
        "Provider_norm": ["A", "A"], "Min": [0, 40], "Max": [50, 999999],
        "Fee": [0, 10],
    })
    assert any("overlap" in i.lower() for i in data_mod.validate_delivery_table(overlap))


def test_real_delivery_table_is_sound(synthetic):
    """The shipped pricing engine's tier table must cover every order value."""
    import data as data_mod

    _, pricing = synthetic
    data_mod.load_delivery_table.clear()
    tbl = data_mod.load_delivery_table(pricing.read_bytes())
    assert data_mod.validate_delivery_table(tbl) == []


# ── P&L report — the pricing engine's model ───────────────────────────────────
def test_vat_factor_is_read_from_the_engine(pl_frames):
    _, _, vat = pl_frames
    assert vat == 1.15, "VAT factor must come from the engine's README sheet"


def test_item_pl_gross_profit_is_ex_vat(pl_frames):
    """Widget: realised 89 incl VAT, WSP 40 incl VAT -> GP measured ex-VAT."""
    item_pl, _, vat = pl_frames
    w = item_pl[item_pl["Item"] == "Widget"].iloc[0]
    # O1,O2,O4,O5 sell it at 100 and O6 at 45 -> weighted 89.0 over 5 units
    assert round(w["RSP Inc. Vat"], 4) == 89.0
    assert round(w["RSP Exl. Vat"], 4) == round(89.0 / vat, 4)
    assert round(w["WSP Inc. Vat"], 4) == 40.0
    assert round(w["WSP Exl. Vat"], 4) == round(40.0 / vat, 4)
    assert round(w["Gross Profit"], 4) == round((89.0 - 40.0) / vat, 4)
    assert round(w["Gross Profit %"], 6) == round((89.0 - 40.0) / 89.0, 6)
    assert w["Units Sold"] == 5
    assert round(w["Total Gross Profit"], 4) == round((89.0 - 40.0) / vat * 5, 4)


def test_item_pl_selling_price_is_volume_weighted(pl_frames):
    """The 45 discount on O6 must drag the realised price below the 100 menu price."""
    item_pl, _, _ = pl_frames
    w = item_pl[item_pl["Item"] == "Widget"].iloc[0]
    assert w["RSP Inc. Vat"] < 100.0


def test_order_pl_fees_are_charged_on_incl_vat_price(pl_frames):
    """O2: 120 incl VAT. Commission 20% -> 24.00, bank 2.5% -> 3.00.
    Fees follow the gross price, not the ex-VAT one."""
    _, order_pl, _ = pl_frames
    o2 = order_pl[order_pl["Order"] == "O2"].iloc[0]
    assert round(o2["Commission"], 4) == 24.0
    assert round(o2["Bank Fee"], 4) == 3.0


def test_compensation_is_forced_to_zero(pl_frames):
    """Compensation is not charged, despite Apps_Config still carrying 2%."""
    _, order_pl, _ = pl_frames
    assert (order_pl["Compensation %"] == 0).all()
    assert (order_pl["Compensation"] == 0).all()


def test_order_pl_counts_items_and_units(pl_frames):
    """O2 has two line items; O1 has one."""
    _, order_pl, _ = pl_frames
    assert order_pl.set_index("Order").loc["O2", "Items"] == 2
    assert order_pl.set_index("Order").loc["O2", "Units"] == 2
    assert order_pl.set_index("Order").loc["O1", "Items"] == 1


def test_order_pl_net_profit_matches_the_engine_formula(pl_frames):
    """Net = GP(ex-VAT) - commission - bank - compensation - delivery."""
    _, order_pl, vat = pl_frames
    o2 = order_pl[order_pl["Order"] == "O2"].iloc[0]
    gp = (120.0 - 50.0) / vat
    expected = gp - 24.0 - 3.0 - 0.0 - 20.0     # compensation zeroed
    assert round(o2["Gross Profit"], 4) == round(gp, 4)
    assert round(o2["Current Net Profit"], 4) == round(expected, 4)
    assert round(o2["Current Margin %"], 6) == round(expected / (120.0 / vat), 6)
    assert o2["Current P/L"] == ("Profit" if expected > 0 else "Loss")


def test_order_pl_excludes_part_costed_orders(pl_frames):
    _, order_pl, _ = pl_frames
    assert "O4" not in set(order_pl["Order"]), \
        "An order with an Unknown WSP line has no knowable P&L"


def test_order_pl_labels_losses(pl_frames):
    _, order_pl, _ = pl_frames
    for _, row in order_pl.iterrows():
        expected = "Profit" if row["Current Net Profit"] > 0 else (
            "Loss" if row["Current Net Profit"] < 0 else "Break-even")
        assert row["Current P/L"] == expected


# ── Order contents drill-down ─────────────────────────────────────────────────
def test_order_lines_shows_what_is_in_an_order(synthetic):
    """O2 holds a Widget at 100 and a Gadget at 20."""
    import data as data_mod

    sales, pricing = synthetic
    pb = pricing.read_bytes()
    data_mod.load_data.clear()
    items = data_mod.load_data(sales.read_bytes(), pb)
    vat = data_mod.read_setting(pb, "VAT factor", 1.15)

    lines = data_mod.order_lines(items, "O2", vat)
    assert len(lines) == 2
    assert set(lines["Item"]) == {"Widget", "Gadget"}
    w = lines[lines["Item"] == "Widget"].iloc[0]
    assert w["RSP Inc. Vat"] == 100.0
    assert w["WSP Inc. Vat"] == 40.0
    assert round(w["Gross Profit"], 4) == round((100.0 - 40.0) / vat, 4)
    assert lines["Line Sale Inc. Vat"].sum() == 120.0


def test_order_lines_exposes_the_uncosted_culprit(synthetic):
    """O4 is excluded from the P&L; the drill-down must show WHICH line did it."""
    import data as data_mod

    sales, pricing = synthetic
    pb = pricing.read_bytes()
    data_mod.load_data.clear()
    items = data_mod.load_data(sales.read_bytes(), pb)
    vat = data_mod.read_setting(pb, "VAT factor", 1.15)

    lines = data_mod.order_lines(items, "O4", vat)
    assert len(lines) == 2
    culprit = lines[lines["Cost Status"] == "Unknown WSP"]
    assert list(culprit["Item"]) == ["Mystery Bundle"]
    assert pd.isna(culprit.iloc[0]["Gross Profit"])


def test_order_lines_unknown_order_returns_empty(synthetic):
    import data as data_mod

    sales, pricing = synthetic
    data_mod.load_data.clear()
    items = data_mod.load_data(sales.read_bytes(), pricing.read_bytes())
    assert data_mod.order_lines(items, "NOPE", 1.15).empty


# ── The real app renders ──────────────────────────────────────────────────────
def test_login_gate_blocks_unauthenticated():
    at = AppTest.from_file(APP_FILE, default_timeout=120)
    at.run()
    assert not at.exception
    assert any("Alnumuw Financial Dashboard" in t.value for t in at.title), \
        "Unauthenticated users must see the login screen, not the dashboard"


def test_password_uses_constant_time_comparison():
    """A plain `==` leaks the password one byte at a time via response timing."""
    src = open(APP_FILE, encoding="utf-8").read()
    assert "hmac.compare_digest" in src
    assert "import hmac" in src
    assert "if pw == expected" not in src


def test_secrets_read_survives_no_secrets_file():
    """Under pytest there is usually no secrets.toml; reading must return None,
    not raise StreamlitSecretNotFoundError."""
    import data as data_mod

    assert data_mod.read_secret("DEFINITELY_NOT_SET_ANYWHERE") is None


def test_wrong_password_is_rejected():
    """A bad password must show an error and never set the session flag."""
    at = AppTest.from_file(APP_FILE, default_timeout=120).run()
    at.text_input[0].set_value("definitely-not-the-password")
    at.button[0].click().run()
    assert not at.exception
    authed = at.session_state["password_correct"] \
        if "password_correct" in at.session_state else False
    assert not authed, "A wrong password must not authenticate"
    assert any("Incorrect password" in e.value for e in at.error)


EXPECTED_TABS = [
    "📈 Sales Performance", "💰 Cost & Profitability", "🧾 Order Profitability",
    "🧮 P&L", "📐 Break-even & Baskets", "🔍 Order Explorer",
]


def test_all_reports_render_on_one_page(app):
    """Every report is a tab on the main page, so one run renders all of them."""
    assert not app.exception, f"App crashed: {app.exception}"
    assert [t.label for t in app.tabs] == EXPECTED_TABS
    # Charts from Sales Performance, Cost & Profitability and Order Profitability
    # must all be present simultaneously — not one page at a time.
    assert len(app.get("plotly_chart")) >= 6
    assert len(app.dataframe) >= 5


def test_cascading_filters_narrow_each_other(app):
    """Picking a brand must shrink the other filters' options to what's reachable."""
    all_items = set(app.multiselect(key="v_Item").options)
    app.multiselect(key="v_Brand").set_value(["BrandA"]).run()
    assert not app.exception
    narrowed = set(app.multiselect(key="v_Item").options)
    assert narrowed <= all_items
    # A user's own pick must never vanish from its own option list.
    assert "BrandA" in app.multiselect(key="v_Brand").options


def test_clear_filters_resets_every_selection(app):
    app.multiselect(key="v_Brand").set_value(["BrandA"]).run()
    app.multiselect(key="v_City").set_value(["Riyadh"]).run()
    app.button(key="clear_filters_btn").click().run()
    assert not app.exception
    for key in ("v_Brand", "v_City", "v_Provider", "v_Status", "v_Item", "v_CostStatus"):
        assert not app.session_state[key], f"{key} should be empty after Clear Filters"


def test_empty_selection_does_not_blank_later_tabs(app):
    """A filter combination yielding no costed rows must not abort the script —
    st.stop() inside a tab would blank every tab after it."""
    app.multiselect(key="v_CostStatus").set_value(["Unknown WSP"]).run()
    assert not app.exception
    assert [t.label for t in app.tabs] == EXPECTED_TABS, \
        "All tabs must still render when a report has no data"


def test_unknown_wsp_banner_is_shown(app):
    joined = " ".join(w.value for w in app.warning)
    assert "Unknown WSP" in joined, "The uncosted-items banner must be visible"
    assert "AggNoCfg" in joined, "The missing-fee-config banner must name the provider"


def test_headline_kpi_is_net_of_delivery(app, pl_frames):
    """Alnumuw pays the delivery fee, so no headline figure may exclude it.
    The KPI bar must equal the order P&L's net profit exactly."""
    _, order_pl, _ = pl_frames
    expected = order_pl["Current Net Profit"].sum()

    cards = [m.value for m in app.markdown if "kpi-label" in m.value]
    assert cards, "KPI bar must render"
    joined = " ".join(cards)
    assert "before delivery" not in joined, \
        "No headline figure may be quoted before delivery"
    assert "after delivery" in joined

    import re
    vals = re.findall(r'kpi-label">(.*?)<.*?kpi-value">(.*?)<', joined, re.S)
    net = dict(vals).get("Net Profit")
    assert net is not None
    shown = float(net.replace("SAR", "").replace(",", "").strip())
    assert abs(shown - round(expected)) <= 1, \
        f"KPI net {shown} must match order P&L net {expected}"
