import json
import os
import sys
import pandas as pd
from datetime import date
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


def dismiss_dialog(page):
    page.evaluate("""
        () => {
            var all = Array.from(document.querySelectorAll('*'));
            for (var el of all) {
                if (el.children.length === 0 && el.textContent.trim() === 'United States') {
                    el.click(); break;
                }
            }
        }
    """)
    page.wait_for_timeout(800)
    page.evaluate("""
        () => {
            var all = Array.from(document.querySelectorAll('button, a, div, span'));
            for (var el of all) {
                var t = el.textContent.trim();
                if (t.includes('Accept') && t.includes('Continue') && t.length < 40) {
                    el.click(); break;
                }
            }
        }
    """)
    page.wait_for_timeout(3000)


def scrape_all_rows(page):
    """Scrape holdings table rows across all pages."""
    all_rows = []
    page_num = 1

    while True:
        print("  Scraping page {}...".format(page_num), file=sys.stderr)

        # Extract all rows from table
        rows = page.evaluate("""
            () => {
                var results = [];
                var tables = document.querySelectorAll('table');
                // Find the holdings table (has Ticker, Cusip columns)
                for (var t of tables) {
                    var headers = Array.from(t.querySelectorAll('th'))
                        .map(function(h) { return h.textContent.trim(); });
                    var hasHoldings = headers.some(function(h) {
                        return h.toLowerCase().includes('ticker') ||
                               h.toLowerCase().includes('company') ||
                               h.toLowerCase().includes('cusip');
                    });
                    if (hasHoldings) {
                        var headerMap = {};
                        headers.forEach(function(h, i) { headerMap[i] = h; });

                        var rows = t.querySelectorAll('tbody tr');
                        rows.forEach(function(row) {
                            var cells = row.querySelectorAll('td');
                            if (cells.length < 3) return;
                            var obj = {};
                            cells.forEach(function(cell, i) {
                                obj[headerMap[i] || ('col' + i)] = cell.textContent.trim();
                            });
                            results.push(obj);
                        });
                        break;
                    }
                }
                return results;
            }
        """)

        if not rows:
            print("  No rows found on page {}.".format(page_num), file=sys.stderr)
            break

        print("  Got {} rows.".format(len(rows)), file=sys.stderr)
        all_rows.extend(rows)

        # Try to click "next" pagination button
        clicked_next = page.evaluate("""
            () => {
                // Look for next page button - common patterns
                var candidates = Array.from(document.querySelectorAll(
                    'button, a, [role="button"], li, span'
                ));

                // Strategy 1: aria-label="Next" or "next page"
                for (var el of candidates) {
                    var aria = (el.getAttribute('aria-label') || '').toLowerCase();
                    if (aria === 'next' || aria === 'next page' || aria === 'go to next page') {
                        if (!el.disabled && !el.classList.contains('disabled')) {
                            el.click();
                            return 'next-aria';
                        }
                        return 'next-disabled';
                    }
                }

                // Strategy 2: element containing only ">" or "›" or "»"
                for (var el of candidates) {
                    var t = el.textContent.trim();
                    if ((t === '>' || t === '›' || t === '»' || t === 'Next') &&
                        !el.disabled && !el.classList.contains('disabled') &&
                        el.offsetParent !== null) {
                        el.click();
                        return 'next-symbol';
                    }
                }

                // Strategy 3: SVG chevron-right inside a button
                var svgBtns = Array.from(document.querySelectorAll('button svg'));
                for (var svg of svgBtns) {
                    var btn = svg.closest('button');
                    if (btn && !btn.disabled && btn.offsetParent !== null) {
                        // Check if it's likely a "next" button by position
                        var allBtns = Array.from(
                            document.querySelectorAll('button')
                        ).filter(function(b) { return b.offsetParent !== null; });
                        var idx = allBtns.indexOf(btn);
                        if (idx === allBtns.length - 1) {
                            btn.click();
                            return 'last-visible-btn';
                        }
                    }
                }

                return 'no-next';
            }
        """)

        print("  Next button: {}".format(clicked_next), file=sys.stderr)

        if "disabled" in clicked_next or "no-next" in clicked_next:
            print("  No more pages.", file=sys.stderr)
            break

        page.wait_for_timeout(2000)
        page_num += 1

        if page_num > 50:  # safety limit
            break

    return all_rows


def parse_rows(raw_rows):
    """Convert raw row dicts to holdings records."""

    def find_key(obj, keywords):
        for kw in keywords:
            for k in obj:
                if kw.lower() in k.lower():
                    return k
        return None

    def safe_float(val):
        try:
            import math
            s = str(val).strip().replace(",", "").replace("%", "").replace("$", "")
            v = float(s)
            return None if math.isnan(v) else v
        except (ValueError, TypeError):
            return None

    records = []
    for row in raw_rows:
        if not row:
            continue

        ticker_key  = find_key(row, ["ticker", "symbol"])
        name_key    = find_key(row, ["company", "name", "security"])
        cusip_key   = find_key(row, ["cusip"])
        shares_key  = find_key(row, ["shares", "notional", "principal"])
        mv_key      = find_key(row, ["market value", "marketvalue"])
        weight_key  = find_key(row, ["weight"])

        ticker = str(row.get(ticker_key, "")).strip() if ticker_key else ""
        name   = str(row.get(name_key,   "")).strip() if name_key   else ""
        cusip  = str(row.get(cusip_key,  "")).strip() if cusip_key  else ""

        key = ticker or cusip or name
        if not key or key.lower() in ("nan", "ticker", "company"):
            continue

        records.append({
            "ticker":       ticker or cusip,
            "name":         name,
            "identifier":   cusip,
            "pct_of_fund":  safe_float(row.get(weight_key)) if weight_key else None,
            "quantity":     safe_float(row.get(shares_key)) if shares_key else None,
            "market_value": safe_float(row.get(mv_key))     if mv_key     else None,
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


def process_etf(page, etf_ticker, fund_id, today_str):
    print("Fetching {} (fund ID {})...".format(etf_ticker, fund_id), file=sys.stderr)
    try:
        url = ("https://www.avantisinvestors.com/avantis-investments/"
               "total-holdings/{}/?type=etf").format(fund_id)
        page.goto(url, wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(5000)
        dismiss_dialog(page)

        # Wait for table rows
        page.wait_for_function(
            "() => document.querySelectorAll('table tbody tr').length > 2",
            timeout=30000
        )
        page.wait_for_timeout(2000)

        raw_rows = scrape_all_rows(page)
        records  = parse_rows(raw_rows)

        if not records:
            print("  No holdings parsed.", file=sys.stderr)
            return

        print("  Total: {} holdings.".format(len(records)), file=sys.stderr)
        save_snapshot(records, today_str, etf_ticker)

        prior_path = find_prior_snapshot(today_str, etf_ticker)
        if not prior_path:
            diff_rows = [{
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
            } for r in records]
            diff = {"date": today_str, "ticker": etf_ticker, "prior_date": None, "diff": diff_rows}
        else:
            with open(prior_path) as f:
                prior_data = json.load(f)
            if prior_data["date"] == today_str:
                print("  Already have data -- skipping.", file=sys.stderr)
                return
            diff = compute_diff(records, prior_data["holdings"], today_str, prior_data["date"], etf_ticker)

        data_dir = get_etf_data_dir(etf_ticker)
        with open(os.path.join(data_dir, "diff.json"), "w") as f:
            json.dump(diff, f, indent=2)
        append_history(today_str, diff, etf_ticker)

        changed = sum(1 for r in diff["diff"] if r["status"] == "changed")
        added   = sum(1 for r in diff["diff"] if r["status"] == "added")
        removed = sum(1 for r in diff["diff"] if r["status"] == "removed")
        print("  Done -- {} changed | {} added | {} removed".format(
            changed, added, removed), file=sys.stderr)

    except Exception as e:
        import traceback
        print("  ERROR: {}".format(e), file=sys.stderr)
        traceback.print_exc(file=sys.stderr)


def main():
    today_str = date.today().isoformat()
    today     = date.today()

    if not is_nyse_trading_day(today):
        print("{} is not a NYSE trading day -- skipping.".format(today_str), file=sys.stderr)
        sys.exit(0)

    print("Running for {}...".format(today_str), file=sys.stderr)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()

        # Set US location once on homepage
        print("Setting US location...", file=sys.stderr)
        page.goto("https://www.avantisinvestors.com/", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(4000)
        dismiss_dialog(page)

        for etf_ticker, info in ETFS.items():
            process_etf(page, etf_ticker, info["fund_id"], today_str)

        browser.close()

    print("All done.", file=sys.stderr)


if __name__ == "__main__":
    main()
