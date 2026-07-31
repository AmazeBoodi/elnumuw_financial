# Alnumuw — Food Delivery Financial Dashboard

An interactive Streamlit dashboard that applies the **pricing engine** (costing)
to the **merged sales data**, to show profitability per item and per order across
brands and delivery aggregators.

## Files

| File | Purpose |
|------|---------|
| `app.py` | Streamlit UI — password gate, data source, pages, filters, charts |
| `data.py` | Data loading, normalization, cost matching, P&L calculations |
| `Alnumuw-MergedData.xlsx` | **Sales.** Order lines (**Merged Items**) and order headers (**Merged Orders**) |
| `Alnumuw Pricing Engine.xlsx` | **Costing.** Item cost (**Items**), aggregator fee rates (**Apps_Config**), tiered delivery fees (**Delivery_Fees**) |
| `test_financial_metrics.py` | Regression tests for every metric definition |
| `SETUP_INSTRUCTIONS.md` | Deployment guide (password + Google Drive) |
| `.streamlit/secrets.toml` | Local password / Drive IDs — **gitignored** |
| `requirements.txt` | Python dependencies |

## Setup & Run

```bash
# 1. (optional) create a virtual environment
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

# 2. install dependencies
pip install -r requirements.txt

# 3. set a local password — edit .streamlit/secrets.toml, replacing CHANGE-ME

# 4. launch the dashboard
streamlit run app.py
```

Streamlit opens the app in your browser (default http://localhost:8501) and asks
for the password before showing anything.

### Where the data comes from

Both workbooks are resolved in this order, so the same code runs locally and in
the cloud:

1. **Sidebar upload override** — for testing new data in one session
2. **Google Drive** — when `DRIVE_FILE_ID` / `DRIVE_PRICING_FILE_ID` are set in
   secrets (the deployed setup)
3. **Local files** next to `app.py` — the normal local-development setup

The sidebar always states which source each workbook came from. See
[SETUP_INSTRUCTIONS.md](SETUP_INSTRUCTIONS.md) for deployment.

> `.gitignore` excludes `.streamlit/secrets.toml` and every `.xlsx` / `.csv`, so
> neither the password nor the business data can be pushed to GitHub.

## Tests

```bash
python -m pytest test_financial_metrics.py -v
```

Runs the real app headlessly against a synthetic dataset with hand-computed
values, protecting the cost match, the delivery tier lookup, and the rule that
excludes part-costed orders.

## How the numbers are calculated

### Matching cost to sales

- **Brand normalization** — order brands are mapped to the engine's spellings
  (e.g. `AlHashimi Sweets → Alahshimi`, `Crunchy's → Crunchys`,
  `Cali Pizza Pasta → cali pizza pasta`, `Chahrazad's Bakery → chahrazad`).
- **Provider normalization** — `Hungerstation → HungerStation`,
  `Toyou → ToYou`, `The Chefz → TheChefz`; all others kept as-is.
- **Cost match** (`data.py → match_cost()`) — each sales line joins to the
  engine's `Items` sheet on **normalized brand + item name**: trimmed,
  case-insensitive, and with runs of internal whitespace collapsed. The engine is
  the single source of truth; nothing is inferred from the sale price, so a
  discounted line keeps its true wholesale cost and simply shows a thinner margin
  — which is what you want when judging promotions.

  > **Do not add fuzzy matching.** At 88% similarity the nearest match to
  > `Esh El-bolbol Blueberry 1/2 kilo` is `Esh El-bolbol Rasberry 1/2 kilo` — a
  > different product that would be silently mis-costed. Whitespace collapsing is
  > the only fuzziness that is safe. Real name mismatches belong in the `Items`
  > sheet.
- **Unknown WSP** — lines with no engine entry are flagged `Unknown WSP` and
  excluded from every profit figure. See *Known gaps* below.

### One profit basis, everywhere

The business model is cloud kitchens: Alnumuw buys the food from the kitchen when
an order comes in, and pays the delivery fee. So WSP is a true variable cost and
delivery is a real cost of the order.

Every figure in the dashboard uses the **pricing engine's basis**:

- Gross profit is measured **ex-VAT** — the VAT collected is remitted, not earned
- Platform fees are charged on the **VAT-inclusive** price — that is what the
  aggregator takes its cut of

| | |
|---|---|
| **Sale Excl VAT** | `Total Sale / VAT factor` |
| **Total Cost** | `WSP incl VAT × Quantity` |
| **Gross Profit** | `Sale Excl VAT − Cost Excl VAT` |
| **Aggregator fees** | `Total Sale (incl VAT) × (Commission % + Bank Fee % + Compensation %)` |
| **Net Profit** | `Gross Profit − Aggregator fees` |
| **Net Margin %** | `Net Profit / Sale Excl VAT` |

Item-level figures are **before delivery**; order-level figures add it. The two
reconcile exactly: item net − delivery = order net, to the riyal.

**Cancellations are costed exactly like completed orders** — same WSP, same fees,
one uniform process. The Order Status filter decides what is counted, not a rule
buried in the code.

### Order-level P&L (Order Profitability)

`data.py → build_orders()` rolls lines up to whole orders and adds the one cost
that only exists at order level:

- **Order Value** = sum of the order's item lines (incl VAT)
- **Delivery Fee** — looked up from `Delivery_Fees`: the band where
  `Min ≤ order value < Max`, for that aggregator
- **Net Profit** = `Order Value − Total Cost − Aggregator fees − Delivery Fee`

Two rules keep this honest:

- An order is only costed when **every** line in it is costed. One uncosted
  bundle makes the whole order's profit unknowable, so such orders are flagged
  and excluded rather than reported as unrealistically profitable. Currently
  **79%** of orders are fully costed.
- Order Value is summed from the item lines rather than taken from the
  `Merged Orders` `Sales` column, because **that column is recorded ex-VAT for
  HungerStation and incl-VAT for every other aggregator** (a ~SAR 44k
  discrepancy across the file). Summing lines keeps one consistent VAT basis and
  makes the order page reconcile exactly with the item pages.

### VAT

Costs (`WSP incl VAT`) and sales (`Total Amount`) are both VAT-inclusive, and the
engine charges aggregator fees on the VAT-inclusive price, so everything stays on
one consistent incl-VAT basis with no conversion.

## Known gaps

**Uncosted items (~13% of lines, ~SAR 215k of revenue).** Almost all are bundles
and offers — *Combo Pizza*, *Kunafa Royal Box*, *Oriental Sweet Offers*,
*Ramadan Exclusive Bundle*, *Victory Duo Box* — sold as one product but made of
several. They cannot be costed until someone maps each bundle to its component
items. Brand **Fananees** is also absent from the engine entirely. The
*Uncosted items* list on the Cost & Profitability page ranks every gap by the
revenue at stake, and exports to CSV, so the highest-value ones can be defined
first. Adding a row to the engine's `Items` sheet is all it takes for an item to
flow into every figure.

**Noon** has no `Apps_Config` row and no `Delivery_Fees` bands, so its orders
carry zero aggregator and delivery cost. Its margin is overstated; a banner says
so whenever it is in the selection.

**Duplicate lines.** 61 order lines are exact repeats — same order, item,
quantity and amount. They are flagged and listed (with CSV export) but never
dropped, because a customer really can order the same thing twice. Check a few
against an aggregator statement; if they are not genuine, the sales export needs
de-duplicating upstream.

**Delivery tier coverage is validated on every load.** A gap in `Delivery_Fees`
would read as free delivery and quietly inflate profit, so any gap or overlap
raises a red banner naming the aggregator and the range.

**Fixed costs are not absorbed.** Rent, salaries, licenses and software (~SAR 56k
/month in the engine's *Fixed Costs & Contribution* sheet) are deliberately
excluded. Every figure here is **contribution**, not a bottom line.

## Reports

Every report lives on the **main page as a tab** — there is no page selector, so
switching report never reloads or loses your filters.

1. **📈 Sales Performance** — monthly sales trend, sales vs net profit by
   aggregator, top 10 items by revenue.
2. **💰 Cost & Profitability** — the uncosted-items worklist, sales composition
   (cost / fees / net profit) per brand, item sale-price vs net-margin scatter
   (bubble = volume), and a Brand × Aggregator P&L table.
3. **🧾 Order Profitability** — where every riyal goes per aggregator, the
   distribution of order net margin, average net profit by basket size, an Order
   P&L by aggregator table, and a per-order detail table (sorted worst-first,
   with a loss-making-only toggle) with CSV export.
4. **🔍 Order Explorer** — searchable/filterable line-item table with CSV export.

A **KPI bar** (Total Sales, Orders, Avg Order Value, Net Profit, Item Net Margin)
sits above the tabs and responds to all filters. Order Profitability adds its own
order-level KPI row (Costed Orders, Order Revenue, Delivery Cost, Net Profit, Net
Margin) that *is* net of delivery.

## Filtering

Same model as the Alnumuw Sales Dashboard.

**Date controls sit at the top of the main page** — a range picker, a
**📅 Reset Date** button, and quick presets (7D · 30D · This Month · Last Month ·
All). Presets are anchored to the latest date *in the data*, not today's calendar
date, so "last 30 days" still works when an upload lags by a day or two.

**The sidebar 🔍 Filters Panel** holds Brand · Aggregator · City · Order Status ·
Item · Cost Status. Each is an expander showing its active selection count, with
a **🧹 Clear Filters** button that resets them all at once (the date range is left
alone).

Filters **cascade bidirectionally**: every filter's option list is narrowed by
the date window and by every *other* filter's selection, so you can't build a
combination that returns nothing — picking Brand = Olivia cuts the item list from
248 to 29. Your own picks are always kept in their own list, so a selection never
silently disappears when you change something else.

## Theming

The dashboard follows the viewer's light/dark preference. Nothing hardcodes a
background: chart templates switch between `plotly_white` and `plotly_dark`, the
accent palette swaps to deeper tones on white, and the KPI cards use translucent
grey with inherited text colour so they read on either canvas.

## Notes

- Currency is displayed as **SAR**.
- Profit and margins are computed only over costed items/orders, so they
  represent the cost-covered portion of the business.
- Large tables are capped at 2,000 rows on screen (pandas' styler limit); CSV
  exports always cover the full filtered set.
