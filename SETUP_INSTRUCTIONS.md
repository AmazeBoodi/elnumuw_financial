# Alnumuw Financial Dashboard — Setup Guide

This guide walks you through the one-time setup needed to deploy the dashboard
with the password gate and Google Drive data loading.

It follows the same structure as the Alnumuw Sales Dashboard, with one
difference: this app reads **two** workbooks, so there are **two** Drive file
IDs to configure.

You will:

1. Upload both Excel workbooks to Google Drive and copy their file IDs
2. Add the file IDs and a password to Streamlit Cloud Secrets
3. Push the code to GitHub
4. Test the deployed app

---

## Step 1 — Upload the two workbooks to Google Drive

The dashboard needs both:

| Workbook | What it holds |
|---|---|
| `Alnumuw-MergedData.xlsx` | **Sales** — `Merged Items`, `Merged Orders` |
| `Alnumuw Pricing Engine.xlsx` | **Costing** — `Items`, `Apps_Config`, `Delivery_Fees` |

For each one:

1. Open Google Drive and upload the file.
2. Right-click → **Share** → set access to **"Anyone with the link"** → role
   **"Viewer"** → **Done**.
3. Right-click again → **Copy link**. It looks like:

   ```
   https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456/view?usp=sharing
   ```

4. The **file ID** is the string between `/d/` and `/view` — in the example
   above, `1AbCdEfGhIjKlMnOpQrStUvWxYz0123456`.

Save both IDs — you'll paste them into secrets next.

**Important:** the share setting must be "Anyone with the link can view" or the
app cannot download the file. Drive does not require sign-in for this mode, but
the file is only discoverable to someone who already has the ID — effectively
just your deployed app.

---

## Step 2 — Add secrets to Streamlit Cloud

1. Go to https://share.streamlit.io and open your app.
2. Click **⋯ → Settings → Secrets**.
3. Paste the following, replacing all three placeholder values:

   ```toml
   APP_PASSWORD = "ChooseAStrongPasswordHere"
   DRIVE_FILE_ID = "sales-workbook-file-id"
   DRIVE_PRICING_FILE_ID = "pricing-engine-file-id"
   ```

4. Click **Save**. Streamlit restarts the app automatically.

**Tip:** pick a password not used anywhere else. You can change it any time by
editing the secrets — no code change needed.

---

## Step 3 — Push the code to GitHub

`.gitignore` already excludes `.streamlit/secrets.toml` and every `.xlsx` /
`.csv`, so neither your password nor your business data can be pushed by
accident. **Check `git status` before your first commit** to confirm no
spreadsheet is listed.

```bash
git add app.py data.py requirements.txt README.md SETUP_INSTRUCTIONS.md .gitignore test_financial_metrics.py
git commit -m "Financial dashboard: password gate and Drive data loading"
git push
```

Streamlit Cloud detects the push and redeploys, usually within 1–2 minutes.

---

## Step 4 — Test the deployed app

1. Open the Streamlit Cloud URL in an incognito window so you start fresh.
2. You should see the **login screen** — enter the password from step 2.
3. The app downloads both workbooks from Drive (you'll see a
   "📥 Downloading latest data from Google Drive…" spinner) and renders.
4. Check the **sidebar**, which should show:
   - 🔄 **Refresh data from Drive** — pulls the latest files
   - ⚙️ **Upload custom files (override)** — test different data without touching Drive
   - 🚪 **Log out** — clears the password and returns to the login screen
   - A source line confirming where each workbook came from (`☁️ Google Drive`)
5. Confirm the two data-quality banners appear, and that the **Order
   Profitability** page renders its delivery-fee figures.

---

## Daily workflow from now on

When you have new data to publish:

1. In Drive, click the existing file → **⋮ → Manage versions → Upload new
   version**. This keeps the same file ID, so nothing else needs to change.
2. In the dashboard, click 🔄 **Refresh data from Drive**.
3. The new numbers appear immediately.

The same applies when you update costs — re-upload a new version of
`Alnumuw Pricing Engine.xlsx` and refresh. Adding a row to its `Items` sheet is
all it takes for a previously **Unknown WSP** product to start being costed.

If you ever replace a file rather than versioning it (new file, new ID), update
the corresponding ID in secrets.

---

## Local development

Two options, no Drive needed:

**Simplest** — keep both `.xlsx` files next to `app.py`. With no `DRIVE_FILE_ID`
in secrets the app reads them directly, and the sidebar shows
`📁 local file`.

You still need a password. Create `.streamlit/secrets.toml`:

```toml
APP_PASSWORD = "your-local-password"
```

That file already exists with a placeholder of `CHANGE-ME` — edit it before
first use. It is gitignored.

**Or point at Drive locally** by adding the two `DRIVE_*` IDs to the same file.

Run with:

```bash
streamlit run app.py
```

> On this machine, use `streamlit run` rather than `python -m streamlit` — bare
> `python` resolves to a 3.11 install that doesn't have Streamlit, while
> `streamlit.exe` belongs to Python 3.13 where everything is installed.

---

## Tests

```bash
python -m pytest test_financial_metrics.py -v
```

These run the real `app.py` headlessly against a small synthetic dataset with
hand-computed expected values, so any edit that silently changes a metric
definition — the cost match, the delivery tier lookup, the rule excluding
part-costed orders — fails loudly.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "APP_PASSWORD is not configured" | Secrets weren't saved | Re-check Settings → Secrets on Streamlit Cloud, or create `.streamlit/secrets.toml` locally |
| "Sales: no data source available" | No Drive ID and no local file | Add `DRIVE_FILE_ID` to secrets, or put the `.xlsx` next to `app.py` |
| "Pricing engine: no data source available" | Same, for the costing workbook | Add `DRIVE_PRICING_FILE_ID` |
| "Google Drive returned an empty response" | File share is "Restricted" | Change share to "Anyone with the link can view" |
| Old data after a Drive update | Cache hasn't expired (1 hour) | Click 🔄 Refresh data from Drive |
| `background_gradient requires matplotlib` | `matplotlib` not installed | `pip install -r requirements.txt` |
| Everything shows as **Unknown WSP** | Pricing engine workbook is the wrong file, or its `Items` sheet was renamed | Check the sidebar source line, and that the sheet is still called `Items` |
| Memory error on Streamlit Cloud | Data too large for the free 1 GB tier | Upgrade hosting, or switch to Parquet / DuckDB |
