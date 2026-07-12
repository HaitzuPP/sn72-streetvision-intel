#!/usr/bin/env python3
"""
SN72 (StreetVision by NATIX) — per-wallet trade / PnL backfill.

Produces trades_data.json, consumed by analytics.html and transactions.html.

Source: taostats dTAO delegation events (/api/delegation/v1). Each stake add
(DELEGATE = buy alpha, TAO into pool) / remove (UNDELEGATE = sell alpha, TAO out)
carries the alpha amount, TAO amount, and the executed USD value, so per-trade price
and cash-flow PnL are reconstructed directly with no external price join.

PnL is cash-flow basis, matching the original build:
    avg_buy_price = invested_usd / alpha_bought
    realized      = proceeds_usd - avg_buy_price * alpha_sold
    unrealized    = current_value_usd - (invested_usd - avg_buy_price * alpha_sold)
    total         = proceeds_usd + current_value_usd - invested_usd
    roi_pct       = total / invested_usd * 100

Wallets featured: the top FEATURE_N alpha holders from data.json (owner excluded so
the page profiles market participants, not the treasury). Current holdings and price
come from data.json, which refresh.py must have written first.

Usage:
    TAOSTATS_API_KEY=... python3 backfill_trades.py
"""
import json, os, sys, time, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone

TAOSTATS_API_KEY = os.environ.get("TAOSTATS_API_KEY", "")
TS_BASE = "https://api.taostats.io"
NETUID = 72
FEATURE_N = 3          # number of top holders to profile
MAX_PAGES = 5          # cap pages of events per wallet (200/page) to bound runtime + rate use
PER_PAGE = 200
_TS_DELAY = 12.0       # taostats key allows ~5 req/min; 12s spacing stays under the cap
_ts_last = [0.0]
# taostats sits behind Cloudflare, which blocks the default urllib User-Agent with a 1010
# bot-challenge (HTTP 403). A browser UA is required for the request to reach the API.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def ts_get(path, **params):
    """GET a taostats endpoint with pacing + 403/429 backoff. Returns dict or None."""
    if not TAOSTATS_API_KEY:
        return None
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = TS_BASE + path + ("?" + q if q else "")
    for attempt in range(6):
        gap = time.time() - _ts_last[0]
        if gap < _TS_DELAY:
            time.sleep(_TS_DELAY - gap)
        _ts_last[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={"Authorization": TAOSTATS_API_KEY,
                                                       "accept": "application/json",
                                                       "User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                time.sleep(5 + attempt * 3)
                continue
            print("  taostats %s -> HTTP %s" % (path, e.code), file=sys.stderr)
            return None
        except Exception:
            time.sleep(3)
    return None


def fnum(x):
    """Coerce a stringy number to float, defaulting to 0.0."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def fetch_events(coldkey):
    """All SN72 stake add/remove events for one coldkey (capped at MAX_PAGES)."""
    events = []
    truncated = False
    for page in range(1, MAX_PAGES + 1):
        d = ts_get("/api/delegation/v1", netuid=NETUID, nominator=coldkey,
                   limit=PER_PAGE, page=page)
        if not d or not d.get("data"):
            break
        for e in d["data"]:
            action = (e.get("action") or "").upper()
            if action not in ("DELEGATE", "UNDELEGATE"):
                continue
            events.append({
                "ts": (e.get("timestamp") or "")[:10],
                "side": "buy" if action == "DELEGATE" else "sell",
                "alpha": fnum(e.get("alpha")) / 1e9,
                "tao": fnum(e.get("amount")) / 1e9,
                "price_usd": round(fnum(e.get("alpha_price_in_usd")), 4),
                "usd": round(fnum(e.get("usd"))),
            })
        if not (d.get("pagination") or {}).get("next_page"):
            break
        if page == MAX_PAGES:
            truncated = True
    events.sort(key=lambda t: t["ts"])
    return events, truncated


def summarise(events, current_alpha, price_usd):
    """Roll per-trade events into the analytics/transactions wallet schema."""
    buys = [e for e in events if e["side"] == "buy"]
    sells = [e for e in events if e["side"] == "sell"]
    alpha_bought = sum(e["alpha"] for e in buys)
    alpha_sold = sum(e["alpha"] for e in sells)
    invested_usd = sum(e["usd"] for e in buys)
    proceeds_usd = sum(e["usd"] for e in sells)
    invested_tao = sum(e["tao"] for e in buys)
    proceeds_tao = sum(e["tao"] for e in sells)
    avg_buy = invested_usd / alpha_bought if alpha_bought else 0.0
    current_value_usd = current_alpha * price_usd
    realized = proceeds_usd - avg_buy * alpha_sold
    cost_of_held = invested_usd - avg_buy * alpha_sold
    unrealized = current_value_usd - cost_of_held
    total = proceeds_usd + current_value_usd - invested_usd
    roi = (total / invested_usd * 100) if invested_usd else 0.0

    monthly = {}
    for e in events:
        m = e["ts"][:7]
        row = monthly.setdefault(m, {"month": m, "buy_usd": 0.0, "sell_usd": 0.0, "_ba": 0.0})
        if e["side"] == "buy":
            row["buy_usd"] += e["usd"]; row["_ba"] += e["alpha"]
        else:
            row["sell_usd"] += e["usd"]
    monthly_rows = []
    for m in sorted(monthly):
        row = monthly[m]
        row["avg_buy_usd"] = (row["buy_usd"] / row["_ba"]) if row["_ba"] else 0.0
        row.pop("_ba")
        monthly_rows.append(row)

    recent = [{"ts": e["ts"], "side": e["side"], "alpha": round(e["alpha"], 1),
               "price_usd": e["price_usd"], "usd": e["usd"]}
              for e in sorted(events, key=lambda t: t["ts"], reverse=True)[:40]]

    return {
        "current_alpha": current_alpha, "current_value_usd": current_value_usd,
        "n_buys": len(buys), "n_sells": len(sells), "n_trades": len(events),
        "alpha_bought": alpha_bought, "alpha_sold": alpha_sold,
        "invested_usd": invested_usd, "proceeds_usd": proceeds_usd,
        "invested_tao": invested_tao, "proceeds_tao": proceeds_tao,
        "avg_buy_price_usd": avg_buy, "realized_pnl_usd": realized,
        "unrealized_pnl_usd": unrealized, "total_pnl_usd": total, "roi_pct": roi,
        "first_trade": events[0]["ts"] if events else None,
        "monthly": monthly_rows, "recent": recent,
    }


def main():
    with open("data.json") as fh:
        data = json.load(fh)
    price_usd = data["subnet"]["price_usd"]
    price_tao = data["subnet"]["price_tao"]
    tao_usd = data["tao"]["price_usd"]
    # top holders excluding the tagged subnet owner (treasury)
    holders = [h for h in data["holders"] if h.get("label") != "SN72 owner"][:FEATURE_N]

    wallets = []
    for h in holders:
        ck = h["coldkey"]
        events, truncated = fetch_events(ck)
        if not events:
            continue
        label = h.get("label") or ("Wallet #%d" % h["rank"])
        key = "".join(c for c in label.lower() if c.isalnum()) or ("w%d" % h["rank"])
        note = "rank #%d%s" % (h["rank"], " · %s" % ("truncated history" if truncated else "full history"))
        w = {"name": label, "key": key, "coldkeys": [ck], "note": note}
        w.update(summarise(events, h["alpha"], price_usd))
        wallets.append(w)
        print("  %s (#%d): %d trades%s" % (label, h["rank"], len(events),
              " [truncated]" if truncated else ""), file=sys.stderr)

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price_tao": price_tao, "price_usd": price_usd, "tao_usd": tao_usd,
        "source": "taostats dTAO delegation events (SN72 stake add/remove) + executed USD. "
                  "PnL on a cash-flow basis (rewards/emissions count as gains).",
        "wallets": wallets,
    }
    with open("trades_data.json", "w") as fh:
        json.dump(out, fh, indent=1)
    print("trades_data.json @ %s | %d wallets profiled" % (out["generated_at"], len(wallets)))


if __name__ == "__main__":
    main()
