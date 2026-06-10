#!/usr/bin/env python3
"""Global Rates & FX Monitor — Standalone Data Fetcher for GitHub Actions

Self-contained script (no external config dependency).
Fetches FX rates from Frankfurter API (ECB) and US Treasury yields.
Optionally fetches FRED data if FRED_API_KEY env var is set.

Produces:
  data/snapshot.json — latest values
  data/history.json  — full time series (appended daily)
"""

import json, os, sys, logging
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("rates-monitor")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_PATH = DATA_DIR / "snapshot.json"
HISTORY_PATH = DATA_DIR / "history.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
REQUEST_TIMEOUT = 20
USER_AGENT = "RatesMonitor/1.0 (GitHub Actions)"

# ── Inline config ─────────────────────────────────────────────
CURRENCIES = [
    "EUR","JPY","GBP","CHF","AUD","CAD","NZD","KRW","INR",
    "SGD","HKD","TWD","BRL","MXN","RUB","CNY",
]

FRED_SERIES = {
    "fed_funds": "FEDFUNDS",
    "ecb_refi": "ECBMRRFR",
    "de_10y": "IRLTLT01DEM156N",
    "jp_10y": "IRLTLT01JPM156N",
    "vix": "VIXCLS",
    "us_10y_breakeven": "T10YIE",
    "us_5y_breakeven": "T5YIE",
    "em_bond_index": "BAMLCC0A0CMTRIV",
}

SEED_VALUES = {
    "boj_rate": 0.50,
    "boe_rate": 4.25,
    "rba_rate": 4.10,
    "boc_rate": 2.75,
    "snb_rate": 0.50,
    "rbi_rate": 6.00,
    "bok_rate": 2.75,
    "china_lpr1y": 3.10,
    "china_lpr5y": 3.60,
    "cn_2y": 1.55,
    "cn_10y": 1.80,
}

# ── Data source functions ─────────────────────────────────────

def fetch_fx_rates() -> dict:
    all_codes = list(set(CURRENCIES + ["USD"]))
    log.info(f"Fetching FX rates for {len(all_codes)} currencies…")
    resp = requests.get(
        "https://api.frankfurter.app/latest",
        params={"from": "USD", "to": ",".join(all_codes)},
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    data = resp.json()
    usd_rates = data["rates"]
    date_str = data["date"]
    result = {"date": date_str, "rates": {"usd": usd_rates}}
    result["_meta"] = {"source": "Frankfurter API (ECB)", "fetched_at": datetime.now().isoformat()}
    log.info(f"  ✓ FX fetched (date: {date_str})")
    return result


def fetch_us_treasury_yields() -> dict:
    log.info("Fetching US Treasury yields…")
    year = datetime.now().year
    url = (
        f"https://home.treasury.gov/resource-center/data-chart-center/"
        f"interest-rates/daily-treasury-rates.csv/{year}/all"
        f"?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
    )
    resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    lines = resp.text.strip().splitlines()
    if len(lines) < 2:
        return {"yields": {}, "date": None}
    header = [h.strip().strip('"') for h in lines[0].split(",")]
    col_map = {"us_2y": "2 Yr", "us_5y": "5 Yr", "us_10y": "10 Yr", "us_30y": "30 Yr"}
    latest_row = lines[1]
    values = [v.strip().strip('"') for v in latest_row.split(",")]
    row_date = values[0]
    yields = {}
    for k, col_name in col_map.items():
        if col_name in header:
            idx = header.index(col_name)
            val = values[idx] if idx < len(values) else ""
            yields[k] = float(val) if val else None
    log.info(f"  ✓ US yields: 2Y={yields.get('us_2y')}, 10Y={yields.get('us_10y')}")
    return {"yields": yields, "date": row_date}


def fetch_fred_value(series_id: str) -> Optional[float]:
    if not FRED_API_KEY:
        return None
    try:
        resp = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": series_id, "api_key": FRED_API_KEY,
                    "file_type": "json", "sort_order": "desc", "limit": 1},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        obs = data.get("observations", [])
        if obs and obs[0]["value"] != ".":
            return float(obs[0]["value"])
    except Exception as e:
        log.debug(f"  ⚠ FRED {series_id}: {e}")
    return None


def fetch_all_fred() -> dict:
    if not FRED_API_KEY:
        log.info("  ℹ FRED_API_KEY not set — skipping FRED data")
        return {}
    log.info("Fetching FRED data…")
    results = {}
    for rate_id, series_id in FRED_SERIES.items():
        val = fetch_fred_value(series_id)
        if val is not None:
            results[rate_id] = val
    log.info(f"  ✓ {len(results)} FRED series fetched")
    return results


# ── History management ────────────────────────────────────────

def load_history() -> dict:
    if HISTORY_PATH.exists():
        with open(HISTORY_PATH) as f:
            return json.load(f)
    return {"fx": {}, "rates": {}, "meta": {"created": datetime.now().isoformat()}}


def save_history(history: dict):
    for key in ["fx", "rates"]:
        if isinstance(history.get(key), dict):
            history[key] = dict(sorted(history[key].items()))
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def save_snapshot(snapshot: dict):
    with open(SNAPSHOT_PATH, "w") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)


def build_snapshot(history: dict) -> dict:
    snapshot = {
        "fx": {}, "rates": {}, "indicators": {}, "market_indicators": {},
        "alerts": [],
        "meta": {"updated": datetime.now().isoformat(), "source": "Rates Monitor (GH Actions)"},
    }

    # FX latest
    if history.get("fx"):
        dates = sorted(history["fx"].keys())
        latest = dates[-1]
        snapshot["fx"]["date"] = latest
        snapshot["fx"]["rates"] = history["fx"][latest]

        # Changes
        def pct_change(latest_dict, prev_dict):
            if not prev_dict: return {}
            return {k: round((v - prev_dict[k]) / prev_dict[k] * 100, 2) if v and prev_dict.get(k) and prev_dict[k] != 0 else None for k, v in latest_dict.items()}

        usd_latest = history["fx"][latest].get("usd", {})
        if len(dates) >= 2:
            d1 = dates[-2]
            snapshot["fx"]["change_1d"] = pct_change(usd_latest, history["fx"][d1].get("usd", {}))
        if len(dates) >= 8:
            d7 = dates[-8]
            snapshot["fx"]["change_7d"] = pct_change(usd_latest, history["fx"][d7].get("usd", {}))
        if len(dates) >= 22:
            d30 = dates[-22]
            snapshot["fx"]["change_1m"] = pct_change(usd_latest, history["fx"][d30].get("usd", {}))

    # Rates: merge latest values
    if history.get("rates"):
        rate_dates = sorted(history["rates"].keys(), reverse=True)
        if rate_dates:
            merged = {}
            for dt in rate_dates:
                for k, v in history["rates"][dt].items():
                    if k not in merged:
                        merged[k] = v
            snapshot["rates"]["date"] = rate_dates[0]
            snapshot["rates"]["values"] = merged

            # Weekly change
            prev = history["rates"].get(rate_dates[min(5, len(rate_dates)-1)], {})
            snapshot["rates"]["change_1w"] = {}
            for k, v in merged.items():
                if k in prev and prev[k] is not None:
                    snapshot["rates"]["change_1w"][k] = round(v - prev[k], 3)

    # Indicators
    rates = snapshot["rates"].get("values", {})
    if rates.get("us_10y") and rates.get("us_2y"):
        snapshot["indicators"]["us_2s10s"] = round((rates["us_10y"] - rates["us_2y"]) * 100, 1)
    if rates.get("cn_10y") and rates.get("us_10y"):
        snapshot["indicators"]["cn_us_10y"] = round((rates["cn_10y"] - rates["us_10y"]) * 100, 1)

    # Market indicators
    mi_keys = ["vix", "us_10y_breakeven", "us_5y_breakeven", "em_bond_index"]
    mi_vals = {k: v for k, v in rates.items() if k in mi_keys and v is not None}
    if mi_vals:
        snapshot["market_indicators"] = {"date": snapshot["rates"].get("date", ""), "values": mi_vals}

    # Alerts
    if "change_1d" in snapshot.get("fx", {}):
        for curr, change in snapshot["fx"]["change_1d"].items():
            if change is not None:
                if abs(change) >= 1.0:
                    snapshot["alerts"].append({"type": "warning", "message": f"{curr}/USD moved {change:+.2f}% today"})
                elif abs(change) >= 0.5:
                    snapshot["alerts"].append({"type": "info", "message": f"{curr}/USD moved {change:+.2f}% today"})

    return snapshot


def daily_update():
    log.info("=" * 50)
    log.info("RATES MONITOR — Daily Update")
    log.info("=" * 50)
    history = load_history()

    # FX
    try:
        fx = fetch_fx_rates()
        dt = fx["date"]
        history.setdefault("fx", {})[dt] = {"usd": fx["rates"]["usd"]}
        log.info(f"  ✓ FX appended for {dt}")
    except Exception as e:
        log.error(f"  ✗ FX failed: {e}")

    # Treasury yields
    try:
        tr = fetch_us_treasury_yields()
        if tr["date"] and tr["yields"]:
            dt_iso = datetime.strptime(tr["date"], "%m/%d/%Y").strftime("%Y-%m-%d")
            history.setdefault("rates", {}).setdefault(dt_iso, {}).update(tr["yields"])
    except Exception as e:
        log.error(f"  ✗ Treasury failed: {e}")

    # FRED
    try:
        fred = fetch_all_fred()
        if fred:
            today = date.today().isoformat()
            history.setdefault("rates", {}).setdefault(today, {}).update(fred)
    except Exception as e:
        log.error(f"  ✗ FRED failed: {e}")

    # Seed values for manual rates
    today = date.today().isoformat()
    for rid, val in SEED_VALUES.items():
        for dt_candidate in sorted(history.get("rates", {}).keys(), reverse=True):
            if rid not in history["rates"][dt_candidate]:
                history["rates"][dt_candidate][rid] = val
                break

    save_history(history)
    snapshot = build_snapshot(history)
    save_snapshot(snapshot)

    alerts = snapshot.get("alerts", [])
    log.info(f"SUMMARY — FX: {len(history.get('fx',{}))}d, Rates: {len(history.get('rates',{}))}d, Alerts: {len(alerts)}")
    return snapshot


if __name__ == "__main__":
    if "--init" in sys.argv:
        log.info("INIT: Fetching full 24-month FX history…")
        today = date.today()
        start = today - timedelta(days=730)
        resp = requests.get(
            f"https://api.frankfurter.app/{start}..{today}",
            params={"from": "USD", "to": ",".join(set(CURRENCIES + ["USD"]))},
            timeout=120, headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()
        history = {"fx": {}, "rates": {}}
        for dt, rates in data["rates"].items():
            history["fx"][dt] = {"usd": rates}
        log.info(f"  ✓ {len(history['fx'])} FX dates loaded")
        save_history(history)
        snapshot = build_snapshot(history)
        save_snapshot(snapshot)
        log.info("INIT COMPLETE")
    else:
        daily_update()
