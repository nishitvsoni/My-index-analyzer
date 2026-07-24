"""
Morning Levels - single self-contained script.

Everything needed (NSE data fetch, pivot/VWAP/option-chain math, Telegram
send) lives in this one file on purpose - it avoids needing GitHub's
folder-upload support at all, since mobile uploads can't reliably recreate
nested folder structures. Just this one file, plus the workflow yml,
is the whole system.
"""

import os
import time
import requests
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional


# ----------------------------------------------------------------------
# NSE data fetching (free, no broker account)
# ----------------------------------------------------------------------

BASE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}
SYMBOL_ENDPOINTS = {
    "NIFTY": "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%2050",
    "BANKNIFTY": "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20BANK",
}
PREOPEN_ENDPOINT = "https://www.nseindia.com/api/market-data-pre-open?key=NIFTY"
OPTION_CHAIN_ENDPOINT = "https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"


def make_nse_session():
    session = requests.Session()
    session.headers.update(BASE_HEADERS)
    session.get("https://www.nseindia.com", timeout=10)
    time.sleep(1)
    return session


def fetch_previous_day_ohlc(symbol):
    session = make_nse_session()
    resp = session.get(SYMBOL_ENDPOINTS[symbol], timeout=10)
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("data", [])
    row = next((r for r in rows if r.get("priority") == 1), rows[0] if rows else None)
    if not row:
        raise RuntimeError("Unexpected NSE response shape - no rows found.")
    return float(row["dayHigh"]), float(row["dayLow"]), float(row["previousClose"])


def fetch_preopen_price(symbol):
    session = make_nse_session()
    resp = session.get(PREOPEN_ENDPOINT, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("data", [])
    row = next((r for r in rows if r.get("metadata", {}).get("symbol") == symbol), None)
    if not row:
        raise RuntimeError("Could not find " + symbol + " in pre-open data.")
    return float(row["metadata"]["lastPrice"])


def fetch_option_chain(symbol, strikes_around_spot=10):
    session = make_nse_session()
    resp = session.get(OPTION_CHAIN_ENDPOINT.format(symbol=symbol), timeout=10)
    resp.raise_for_status()
    data = resp.json()
    records = data.get("records", {})
    spot = records.get("underlyingValue")
    rows = records.get("data", [])
    parsed = []
    for row in rows:
        ce = row.get("CE", {})
        pe = row.get("PE", {})
        if not ce and not pe:
            continue
        parsed.append({
            "strike": row.get("strikePrice"),
            "call_oi": ce.get("openInterest", 0),
            "call_oi_change": ce.get("changeinOpenInterest", 0),
            "put_oi": pe.get("openInterest", 0),
            "put_oi_change": pe.get("changeinOpenInterest", 0),
        })
    if spot and parsed:
        parsed.sort(key=lambda r: abs(r["strike"] - spot))
        parsed = parsed[:strikes_around_spot]
        parsed.sort(key=lambda r: r["strike"])
    return parsed


# ----------------------------------------------------------------------
# Levels math (tested logic - Camarilla/standard pivots, gap, option chain)
# ----------------------------------------------------------------------

@dataclass
class Level:
    label: str
    price: float
    kind: str


def camarilla_pivots(prev_high, prev_low, prev_close):
    rng = prev_high - prev_low
    c = prev_close
    return [
        Level("Camarilla R4", c + rng * 1.1 / 2, "resistance"),
        Level("Camarilla R3", c + rng * 1.1 / 4, "resistance"),
        Level("Camarilla R2", c + rng * 1.1 / 6, "resistance"),
        Level("Camarilla R1", c + rng * 1.1 / 12, "resistance"),
        Level("Camarilla S1", c - rng * 1.1 / 12, "support"),
        Level("Camarilla S2", c - rng * 1.1 / 6, "support"),
        Level("Camarilla S3", c - rng * 1.1 / 4, "support"),
        Level("Camarilla S4", c - rng * 1.1 / 2, "support"),
    ]


def standard_pivots(prev_high, prev_low, prev_close):
    pp = (prev_high + prev_low + prev_close) / 3
    r1 = 2 * pp - prev_low
    s1 = 2 * pp - prev_high
    r2 = pp + (prev_high - prev_low)
    s2 = pp - (prev_high - prev_low)
    r3 = prev_high + 2 * (pp - prev_low)
    s3 = prev_low - 2 * (prev_high - pp)
    return [
        Level("Pivot (PP)", pp, "resistance"),
        Level("Standard R1", r1, "resistance"),
        Level("Standard R2", r2, "resistance"),
        Level("Standard R3", r3, "resistance"),
        Level("Standard S1", s1, "support"),
        Level("Standard S2", s2, "support"),
        Level("Standard S3", s3, "support"),
    ]


def gap_analysis(prev_close, preopen_price):
    gap_points = preopen_price - prev_close
    gap_percent = (gap_points / prev_close) * 100 if prev_close else 0
    if abs(gap_percent) < 0.15:
        gap_type = "Flat / no significant gap"
        note = "Expect range-bound levels to matter more than gap-fill trades."
    elif gap_percent >= 0.15:
        gap_type = "Gap-up"
        note = "Watch prev close (" + format(prev_close, ".2f") + ") as a potential gap-fill support."
    else:
        gap_type = "Gap-down"
        note = "Watch prev close (" + format(prev_close, ".2f") + ") as a potential gap-fill resistance."
    return gap_points, gap_percent, gap_type, note


def analyze_option_chain(rows, oi_change_threshold_pct=15):
    if not rows:
        return None
    total_call_oi = sum(r["call_oi"] for r in rows)
    total_put_oi = sum(r["put_oi"] for r in rows)
    pcr = (total_put_oi / total_call_oi) if total_call_oi else 0
    if pcr >= 1.2:
        pcr_bias = "Bullish bias (PCR high)"
    elif pcr <= 0.8:
        pcr_bias = "Bearish bias (PCR low)"
    else:
        pcr_bias = "Neutral"
    max_call_row = max(rows, key=lambda r: r["call_oi"])
    max_put_row = max(rows, key=lambda r: r["put_oi"])
    levels = [
        Level("Max Call OI (" + str(int(max_call_row["strike"])) + " CE)", max_call_row["strike"], "resistance"),
        Level("Max Put OI (" + str(int(max_put_row["strike"])) + " PE)", max_put_row["strike"], "support"),
    ]
    return {"pcr": round(pcr, 2), "pcr_bias": pcr_bias, "levels": levels}


def find_scalp_zones(levels, min_width=15, stop_loss_buffer=5, current_price=None):
    sorted_levels = sorted(levels, key=lambda lv: lv.price)
    zones = []
    for lower, upper in zip(sorted_levels, sorted_levels[1:]):
        width = upper.price - lower.price
        if width < min_width:
            continue
        zones.append({
            "lower": lower, "upper": upper, "width": round(width, 2),
            "sl": stop_loss_buffer,
        })
    if current_price is not None:
        def distance(z):
            if z["lower"].price <= current_price <= z["upper"].price:
                return 0
            return min(abs(current_price - z["lower"].price), abs(current_price - z["upper"].price))
        zones.sort(key=distance)
    return zones


def build_message(symbol, prev_high, prev_low, prev_close, preopen_price, option_summary):
    levels = camarilla_pivots(prev_high, prev_low, prev_close) + standard_pivots(prev_high, prev_low, prev_close)
    current_price = preopen_price if preopen_price else prev_close

    lines = []
    lines.append("*" + symbol + " Levels - " + date.today().isoformat() + "*")
    lines.append("Prev Close: " + format(prev_close, ".2f") + " | Prev H/L: " + format(prev_high, ".2f") + " / " + format(prev_low, ".2f"))

    if preopen_price:
        gap_points, gap_percent, gap_type, note = gap_analysis(prev_close, preopen_price)
        lines.append("Pre-open: " + format(preopen_price, ".2f") + " (" + gap_type + ", " + format(gap_points, "+.1f") + " pts)")
        lines.append("_" + note + "_")

    if option_summary:
        lines.append("")
        lines.append("*Option Chain:* PCR " + str(option_summary["pcr"]) + " - " + option_summary["pcr_bias"])
        levels += option_summary["levels"]

    lines.append("")
    lines.append("*Key Levels (high to low):*")
    for lv in sorted(levels, key=lambda x: x.price, reverse=True):
        tag = "R" if lv.kind == "resistance" else "S"
        lines.append("  [" + tag + "] " + lv.label + ": " + format(lv.price, ".2f"))

    zones = find_scalp_zones(levels, current_price=current_price)
    lines.append("")
    if zones:
        lines.append("*Scalp Zones (min 15pt target / max 5pt SL):*")
        for i, z in enumerate(zones[:5], 1):
            lines.append("  " + str(i) + ". " + z["lower"].label + " (" + format(z["lower"].price, ".2f") + ") <-> " +
                          z["upper"].label + " (" + format(z["upper"].price, ".2f") + ")  [" + str(z["width"]) + "pt, SL " + str(z["sl"]) + "pt]")
    else:
        lines.append("*No zones meeting the min-15pt/max-5pt rule today.*")

    return "\n".join(lines)


# ----------------------------------------------------------------------
# Telegram delivery
# ----------------------------------------------------------------------

def send_telegram_message(text):
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = "https://api.telegram.org/bot" + bot_token + "/sendMessage"
    # Plain text, no parse_mode: Telegram's Markdown parser is strict about
    # matching * and _ exactly, and a single stray one anywhere in an
    # auto-generated message causes a 400 error. Stripping them and sending
    # plain text means this can never fail for that reason again.
    plain_text = text.replace("*", "").replace("_", "")
    resp = requests.post(url, json={"chat_id": chat_id, "text": plain_text}, timeout=15)
    if not resp.ok:
        print("Telegram error response:", resp.text)
    resp.raise_for_status()
    return resp.json()


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def run_for_symbol(symbol):
    high, low, close = fetch_previous_day_ohlc(symbol)
    try:
        preopen = fetch_preopen_price(symbol)
    except Exception as e:
        print("[warn] pre-open fetch failed for " + symbol + ": " + str(e))
        preopen = None
    try:
        oc_rows = fetch_option_chain(symbol)
        option_summary = analyze_option_chain(oc_rows)
    except Exception as e:
        print("[warn] option chain fetch failed for " + symbol + ": " + str(e))
        option_summary = None
    return build_message(symbol, high, low, close, preopen, option_summary)


def main():
    messages = []
    for symbol in ["NIFTY", "BANKNIFTY"]:
        try:
            messages.append(run_for_symbol(symbol))
        except Exception as e:
            messages.append("*" + symbol + "*: could not complete analysis today - " + str(e))
    full_message = ("\n\n" + ("-" * 20) + "\n\n").join(messages)
    send_telegram_message(full_message)
    print("Sent morning levels to Telegram.")


if __name__ == "__main__":
    main()
