import json
import os
import sys
import tempfile
import pandas as pd
from datetime import date
from io import StringIO
import pandas_market_calendars as mcal
from playwright.sync_api import sync_playwright

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

ETFS = {
    "AVUV": {"fund_id": "119",  "name": "US Small Cap Value ETF"},
    "AVGV": {"fund_id": "737",  "name": "All Equity Markets Value ETF"},
    "AVNM": {"fund_id": "738",  "name": "All International Markets Equity ETF"},
    "AVLV": {"fund_id": "806",  "name": "US Large Cap Value ETF"},
}


def is_nyse_trading_day(d):
    nyse = mcal.get_calendar("NYSE")
    return not nyse.schedule(start_date=d.isoformat(), end_date=d.isoformat()).empty


def download_csv(fund_id, ticker):
    """Use Playwright to download the All Holdings CSV from Avantis."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            accept_downloads=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()

        url = ("https://www.avantisinvestors.com/avantis-investments/"
               "total-holdings/{}/?type=etf").format(fund_id)
        print("  Loading {}...".format(url), file=sys.stderr)
        page.goto(url, wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(5000)

        # Handle country selector — select United States then Accept & Continue
        try:
            us = page.locator("text=United States").first
            if us.is_visible(timeout=3000):
                us.click()
                page.wait_for_timeout(1000)
                print("  Selected United States.", file=sys.stderr)
        except Exception:
            pass

        try:
            accept = page.locator("text=Accept & Continue").first
            if accept.is_visible(timeout=3000):
                accept.click()
                page.wait_for_timeout(5000)
                print("  Accepted terms.", file=sys.stderr)
        except Exception:
            pass

        # Wait for holdings table
        try:
            page.wait_for_selector("table", timeout=30000)
            page.wait_for_timeout(2000)
            print("  Holdings table loaded.", file=sys.stderr)
        except Exception as e:
            print("  Table not found: {}".format(e), file=sys.stderr)

        # Download CSV
        tmp_path = tempfile.mktemp(suffix=".csv")
        try:
            with page.expect_download(timeout=30000) as dl_info:
                page.locator("text=All holdings (CSV)").first.click()
                print("  Clicked CSV download.", file=sys.stderr)

            dl = dl_info.value
            dl.save_as(tmp_path)
            print("  Downloaded CSV.", file=sys.stderr)

            with open(tmp_path, "r", encoding="utf-8") as f:
                csv_text = f.read()

            try:
                os.unlink(tmp_path)
            except Exception:
                pass

            browser.close()
            return csv_text

        except Exception as e:
            print("  Download failed: {}".format(e), file=sys.stderr)
            browser.close()
            return None


def parse_holdings(csv_text):
    """Parse the CSV and return list of holding dicts."""
    if not csv_text or not csv_text.strip():
        return []

    try:
        df = pd.read_csv(StringIO(csv_text))
        df.columns = [c.strip() for c in df.columns]
        print("  CSV columns: {}".format(list(df.columns)), file=sys.stderr)
    except Exception as e:
        print("  CSV parse error: {}".format(e), file=sys.stderr)
        return []

    # Flexible column finder
    def find_col(keywords):
        for kw in keywords:
            for col in df.columns:
                if kw.lower() in col.lower():
                    return col
        return None

    ticker_col = find_col(["ticker", "symbol"])
    name_col   = find_col(["company", "name", "security description", "security"])
    cusip_col  = find_col(["cusip"])
    shares_col = find_col(["shares", "notional", "principal"])
    mv_col     = find_col(["market value", "marketvalue"])
    weight_col = find_col(["weight"])

    def safe_float(val):
        try:
            s = str(val).strip().replace(",", "").replace("%", "").replace("$", "")
            v = float(s)
            import math
            return None if math.isnan(v) else v
        except (ValueError, TypeError):
            return None

    records = []
    for _, row in df.iterrows():
        ticker = str(row[ticker_col]).strip() if ticker_col else ""
        name   = str(row[name_col]).strip()   if name_col   else ""
        cusip  = str(row[cusip_col]).strip()  if cusip_col  else ""

        if ticker.lower() in ("nan", "ticker", ""):
            ticker = ""
        if name.lower()   == "nan":
            name = ""
        if cusip.lower()  == "nan":
            cusip = ""

        # Need at least a ticker or name to be a valid row
        key = ticker or name
        if not key:
            continue

        weight = safe_float(row[weight_col]) if weight_col else None
        # Weight is already in percentage form e.g. 1.05 (meaning 1.05%)

        records.append({
            "ticker":       ticker or cusip,  # fall back to CUSIP if no ticker
            "name":         name,
            "identifier":   cusip,
            "pct_of_fund":  weight,
            "quantity":     safe_float(row[shares_col])  if shares_col else None,
            "market_value": safe_float(row[mv_col])      if mv_col     else None,
            "sector":       "",
        })

    return records


def get_etf_data_dir(etf_ticker):
    d = os.path.join(DATA_DIR, etf_ticker)
    os.makedirs(d, exist_ok=True)
    return d


def save_snapshot(records, today_str, etf_ticker):
    data_dir = get_etf_data_dir(etf_ticker)
    payload = {"date": today_str, "ticker": etf_ticker, "holdings": records}
    with open(os.path.join(data_dir, "{}.json".format(today_str)), "w") as f:
        json.dump(payload, f, indent=2)
    with open(os.path.join(data_dir, "latest.json"), "w") as f:
        json.dump(payload, f, indent=2)


def find_prior_snapshot(today_str, etf_ticker):
    data_dir = get_etf_data_dir(etf_ticker)
    files = sorted(
        f for f in os.listdir(data_dir)
        if f.endswith(".json") and f not in ("latest.json", "diff.json", "history.json")
    )
    prior = [f for f in files if f.replace(".json", "") < today_str]
    return os.path.join(data_dir, prior[-1]) if prior else None


def compute_diff(today_records, prior_records, today_str, prior_date_str, etf_ticker):
    today_map = {r["ticker"]: r for r in today_records}
    prior_map = {r["ticker"]: r for r in prior_records}
    all_keys  = sorted(set(today_map) | set(prior_map))
    rows = []
    for key in all_keys:
        t = today_map.get(key)
        p = prior_map.get(key)
        if t and p:
            q_today   = t["quantity"]    or 0
            q_prior   = p["quantity"]    or 0
            pct_today = t["pct_of_fund"] or 0
            pct_prior = p["pct_of_fund"] or 0
            qty_chg   = ((q_today - q_prior) / q_prior * 100) if q_prior != 0 else 0
            rows.append({
                "ticker":              t["ticker"],
                "name":                t.get("name") or p.get("name") or "",
                "identifier":          t.get("identifier") or "",
                "sector":              "",
                "status":              "changed" if round(qty_chg, 6) != 0 else "unchanged",
                "quantity_today":      q_today,
                "quantity_prior":      q_prior,
                "quantity_pct_change": round(qty_chg, 4),
                "pct_of_fund_today":   pct_today,
                "pct_of_fund_prior":   pct_prior,
                "pct_of_fund_change":  round(pct_today - pct_prior, 4),
                "market_value_today":  t.get("market_value"),
            })
        elif t:
            rows.append({
                "ticker": t["ticker"], "name": t.get("name") or "",
                "identifier": t.get("identifier") or "", "sector": "",
                "status": "added",
                "quantity_today": t["quantity"] or 0, "quantity_prior": None,
                "quantity_pct_change": None,
                "pct_of_fund_today": t["pct_of_fund"] or 0, "pct_of_fund_prior": None,
                "pct_of_fund_change": None, "market_value_today": t.get("market_value"),
            })
        else:
            rows.append({
                "ticker": p["ticker"], "name": p.get("name") or "",
                "identifier": p.get("identifier") or "", "sector": "",
                "status": "removed",
                "quantity_today": None, "quantity_prior": p["quantity"] or 0,
                "quantity_pct_change": None, "pct_of_fund_today": None,
                "pct_of_fund_prior": p["pct_of_fund"] or 0,
                "pct_of_fund_change": None, "market_value_today": None,
            })
    return {"date": today_str, "ticker": etf_ticker, "prior_date": prior_date_str, "diff": rows}


def append_history(today_str, diff, etf_ticker):
    data_dir = get_etf_data_dir(etf_ticker)
    history_path = os.path.join(data_dir, "history.json")
    history = []
    if os.path.exists(history_path):
        with open(history_path) as f:
            history = json.load(f)
    entry = {"date": today_str, "prior_date": diff["prior_date"]}
    if entry not in history:
        history.append(entry)
        history.sort(key=lambda x: x["date"], reverse=True)
    with open(history_path, "w") as f:
        json.dump(history, f, indent=2)


def process_etf(etf_ticker, fund_id, today_str):
    print("Fetching {} (fund ID {})...".format(etf_ticker, fund_id), file=sys.stderr)
    try:
        csv_text = download_csv(fund_id, etf_ticker)
        if not csv_text:
            print("  No CSV data returned.", file=sys.stderr)
            return

        records = parse_holdings(csv_text)
        if not records:
            print("  No holdings parsed.", file=sys.stderr)
            return

        print("  {} holdings found.".format(len(records)), file=sys.stderr)
        save_snapshot(records, today_str, etf_ticker)

        prior_path = find_prior_snapshot(today_str, etf_ticker)
        if not prior_path:
            diff_rows = []
            for r in records:
                diff_rows.append({
                    "ticker":              r["ticker"],
                    "name":                r.get("name") or "",
                    "identifier":          r.get("identifier") or "",
                    "sector":              "",
                    "status":              "unchanged",
                    "quantity_today":      r["quantity"] or 0,
                    "quantity_prior":      r["quantity"] or 0,
                    "quantity_pct_change": 0,
                    "pct_of_fund_today":   r["pct_of_fund"] or 0,
                    "pct_of_fund_prior":   r["pct_of_fund"] or 0,
                    "pct_of_fund_change":  0,
                    "market_value_today":  r.get("market_value"),
                })
            diff = {"date": today_str, "ticker": etf_ticker, "prior_date": None, "diff": diff_rows}
        else:
            with open(prior_path) as f:
                prior_data = json.load(f)
            if prior_data["date"] == today_str:
                print("  Already have data for {} -- skipping.".format(today_str), file=sys.stderr)
                return
            diff = compute_diff(records, prior_data["holdings"], today_str, prior_data["date"], etf_ticker)

        data_dir = get_etf_data_dir(etf_ticker)
        with open(os.path.join(data_dir, "diff.json"), "w") as f:
            json.dump(diff, f, indent=2)

        append_history(today_str, diff, etf_ticker)

        changed = sum(1 for r in diff["diff"] if r["status"] == "changed")
        added   = sum(1 for r in diff["diff"] if r["status"] == "added")
        removed = sum(1 for r in diff["diff"] if r["status"] == "removed")
        print("  Done -- {} holdings | {} changed | {} added | {} removed".format(
            len(records), changed, added, removed), file=sys.stderr)

    except Exception as e:
        print("  ERROR for {}: {}".format(etf_ticker, e), file=sys.stderr)


def main():
    today_str = date.today().isoformat()
    today     = date.today()

    if not is_nyse_trading_day(today):
        print("{} is not a NYSE trading day -- skipping.".format(today_str), file=sys.stderr)
        sys.exit(0)

    print("Running for {}...".format(today_str), file=sys.stderr)
    for etf_ticker, info in ETFS.items():
        process_etf(etf_ticker, info["fund_id"], today_str)
    print("All done.", file=sys.stderr)


if __name__ == "__main__":
    main()
