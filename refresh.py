#!/usr/bin/env python3
"""
SN72 (StreetVision by NATIX) Token Intelligence — data refresher.

Sources:
  - tao.com Data API: price, emissions, holders, validators, subnet leaderboard.
  - taostats API: Bittensor Conviction locks, on-chain identities (validator/exchange
    tags), used to enrich the top-25 holders and the conviction section.

Writes data.json, which index.html / wallets.html load on open. Both APIs need a
signed key and send no CORS headers, so this runs server-side.

Usage:
    python3 refresh.py
    TAOSTATS_API_KEY=... TAO_KEY=... TAO_SECRET=... python3 refresh.py   # env overrides
"""
import json, os, sys, time, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone

# --- credentials (env overrides embedded defaults) ---
TAO_KEY    = os.environ.get("TAO_KEY", "")
TAO_SECRET = os.environ.get("TAO_SECRET", "")
TAOSTATS_API_KEY = os.environ.get("TAOSTATS_API_KEY", "")

TAO_BASE = "https://api.tao.com"
TAO_DATA = TAO_BASE + "/data/v1"
TS_BASE  = "https://api.taostats.io"
NETUID   = 72
TARGETS  = ()             # no pinned comps for SN72 — show rank neighbours only
NEIGHBORS = 3             # ranks above/below SN72 to show

# Manual labels: coldkey -> (label, highlight, is_validator). highlight: green|purple|gold
# The owner coldkey belongs to NATIX, the operating company, which holds the subnet owner
# key and shares owner emission with Yuma Group (DCG) under a JV — see structure.html.
MANUAL_LABELS = {
    "5HTYVBxrF2WbVN8RBtFxAkBGuHJxjgLd9Sze5gxH4KC6GLCv": ("NATIX (owner)", "green", False),
    "5E9fVY1jexCNVMjd2rdBsAxeamFGEMfzHcyTn2fHgdHeYc5p": ("Yuma Group", "gold", True),
}


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ---------------- tao.com ----------------
def tao_auth():
    body = json.dumps({"type": "API_KEY", "username": TAO_KEY, "password": TAO_SECRET}).encode()
    req = urllib.request.Request(TAO_BASE + "/auth", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["access_token"]


def tao_get(token, path):
    req = urllib.request.Request(TAO_DATA + path, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


# ---------------- taostats (rate-limited) ----------------
_TS_DELAY = 12.0  # taostats key allows ~5 req/min; 12s spacing stays under the cap
_ts_last = [0.0]
# taostats sits behind Cloudflare, which blocks the default urllib User-Agent with a 1010
# bot-challenge (HTTP 403). A browser UA is required for the request to reach the API.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def ts_get(path, **params):
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
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):      # bot-challenge / throttle — back off and retry
                time.sleep(3 + attempt * 2)
                continue
            print("  taostats %s -> HTTP %s" % (path, e.code), file=sys.stderr)
            return None
        except Exception as e:
            time.sleep(2)
    return None


def ts_conviction(circ_alpha, price_usd):
    d = ts_get("/api/conviction/latest/v1", netuid=NETUID, limit=200)
    if not d or "data" not in d:
        return {"available": False}
    uniq = {}                              # one lock per coldkey per subnet -> dedup
    for it in d["data"]:
        ck = (it.get("coldkey") or {}).get("ss58", "")
        uniq[ck] = it
    rows = []
    locked_rao = 0
    perp = 0
    for ck, it in uniq.items():
        amt = int(it.get("amount_locked", 0) or 0)
        locked_rao += amt
        if it.get("perpetual"):
            perp += 1
        rows.append({"coldkey": ck, "alpha": amt / 1e9,
                     "perpetual": bool(it.get("perpetual")),
                     "owner_hotkey": bool(it.get("is_owner_hotkey"))})
    rows.sort(key=lambda r: r["alpha"], reverse=True)
    locked = locked_rao / 1e9
    return {"available": True, "locked_alpha": locked, "locked_usd": locked * price_usd,
            "pct_of_circulating": (locked / circ_alpha * 100) if circ_alpha else 0.0,
            "lock_count": len(uniq), "perpetual_count": perp, "top": rows[:8]}


def ts_validators():
    """coldkey -> validator name for the largest network validators (single page).

    The taostats key is capped at ~5 req/min, so this takes one page (top 200 validators
    by stake) rather than the full network. SN72's own validators are also tagged from the
    tao.com validator list in main(), so subnet-level coverage does not depend on this.
    """
    out = {}
    d = ts_get("/api/dtao/validator/latest/v1", limit=200, page=1)
    if d and d.get("data"):
        for v in d["data"]:
            ck = (v.get("coldkey") or {}).get("ss58")
            if ck:
                out[ck] = v.get("name") or "Validator"
    return out


def ts_identity_map():
    """coldkey -> (name, is_validator) from the exchange list (single call).

    Full on-chain identity pagination is skipped to stay within the taostats rate cap;
    named-entity tagging therefore comes from exchanges here plus validators/owner elsewhere.
    """
    out = {}
    ex = ts_get("/api/exchange/v1", limit=200)
    if ex and ex.get("data"):
        for e in ex["data"]:
            ck = (e.get("coldkey") or {}).get("ss58")
            if ck:
                out[ck] = (e.get("name"), False)
    return out


# ---------------- leaderboard (tao.com, ranked by alpha price / FDV) ----------------
def build_leaderboard(token):
    pools = tao_get(token, "/subnet-pools/latest")["_embedded"]["subnet_pools"]
    ranked = sorted(pools, key=lambda p: f(p.get("price_tao")), reverse=True)  # == FDV rank
    for i, p in enumerate(ranked, 1):
        p["_rank"] = i
    by = {p["netuid"]: p for p in ranked}
    sn = by.get(NETUID)
    r = sn["_rank"]
    picks = {}
    for p in ranked:
        if r - NEIGHBORS <= p["_rank"] <= r + NEIGHBORS:
            picks[p["netuid"]] = p
    for uid in TARGETS:
        if uid in by:
            picks.setdefault(uid, by[uid])
    rows = [{"rank": p["_rank"], "netuid": p["netuid"], "name": p["name"],
             "mcap_usd": f(p.get("market_cap_usd")), "change_7d": f(p.get("price_change_percent_1w")),
             "is_subject": p["netuid"] == NETUID, "is_target": p["netuid"] in TARGETS}
            for p in sorted(picks.values(), key=lambda x: x["_rank"])]
    return {"subnet_rank": r, "total": len(ranked), "rows": rows}


# ---------------- build ----------------
def main():
    token = tao_auth()
    tao  = tao_get(token, "/tao/price/latest")
    net  = tao_get(token, "/network/stats/latest")
    sub  = tao_get(token, "/subnets/latest?netuids=%d" % NETUID)["_embedded"]["subnets"][0]
    pool = tao_get(token, "/subnet-pools/latest?netuids=%d" % NETUID)["_embedded"]["subnet_pools"][0]
    emis = tao_get(token, "/subnet-emissions/latest?netuids=%d" % NETUID)["_embedded"]["subnet_emissions"][0]
    vals = tao_get(token, "/subnets/%d/validators/latest" % NETUID)["_embedded"]["validators"]
    hold = tao_get(token, "/subnets/%d/holders/top?limit=25" % NETUID)["_embedded"]["holders"]

    tao_usd   = f(tao["price_usd"])
    price_usd = f(sub["price_usd"])
    price_tao = f(sub["price_tao"])
    circ_alpha = f(sub["market_cap_tao"]) / price_tao if price_tao else 0.0

    leaderboard = build_leaderboard(token)

    # taostats enrichment — conviction first (most valuable) so it gets a fresh rate budget
    conviction = ts_conviction(circ_alpha, price_usd) if TAOSTATS_API_KEY else {"available": False}
    val_map = ts_validators() if TAOSTATS_API_KEY else {}
    id_map = ts_identity_map() if TAOSTATS_API_KEY else {}
    # if taostats couldn't be reached this run, keep the last-good conviction (don't blank it out)
    if not conviction.get("available") and os.path.exists("data.json"):
        try:
            prev = json.load(open("data.json")).get("conviction", {})
            if prev.get("available"):
                prev["stale"] = True
                # revalue the locked alpha at the current price so USD stays sensible
                la = prev.get("locked_alpha", 0)
                prev["locked_usd"] = la * price_usd
                prev["pct_of_circulating"] = (la / circ_alpha * 100) if circ_alpha else prev.get("pct_of_circulating", 0)
                conviction = prev
        except Exception:
            pass
    # SN72 validator owner coldkeys are validators too
    for v in vals:
        if v.get("owner_coldkey"):
            val_map.setdefault(v["owner_coldkey"], v.get("name") or "Validator")

    def emit(v):
        t = f(v)
        return {"tao_day": t, "usd_day": t * tao_usd, "usd_year": t * tao_usd * 365}

    holders = []
    for i, h in enumerate(hold, 1):
        ck = h["coldkey"]
        alpha = f(h["total_alpha_rao"]) / 1e9
        label, hl, is_val = MANUAL_LABELS.get(ck, (None, None, False))
        if ck in val_map:                        # network/SN72 validator
            is_val = True
            if label is None:
                label, hl = val_map[ck], "gold"
        if label is None and ck in id_map:        # named entity / exchange
            nm, v = id_map[ck]
            label, hl = nm, "gold"
            is_val = is_val or v
        holders.append({"rank": i, "coldkey": ck, "alpha": alpha, "usd": alpha * price_usd,
                        "share_pct": (alpha / circ_alpha * 100) if circ_alpha else 0.0,
                        "label": label, "hl": hl, "is_val": bool(is_val)})

    # when each coldkey entered the top 25.
    # holders_entry.json holds dates reconstructed from on-chain SN72 trades (backfilled once);
    # holders_seen.json records first-detected date for any wallet not in that file (going forward).
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    def _load(p):
        try:
            return json.load(open(p))
        except Exception:
            return {}
    entry = _load("holders_entry.json")          # {coldkey: {"entered": "YYYY-MM-DD", "method": ...}}
    seen = _load("holders_seen.json")
    for h in holders:
        ck = h["coldkey"]
        if ck in entry and entry[ck].get("entered"):
            h["entered"] = entry[ck]["entered"]; h["entered_method"] = entry[ck].get("method", "trades")
        else:
            seen.setdefault(ck, today)
            h["entered"] = seen[ck]; h["entered_method"] = "first-seen"
        try:
            days = (datetime.now(timezone.utc) - datetime.fromisoformat(h["entered"] + "T00:00:00+00:00")).days
            h["is_new"] = days <= 45
        except Exception:
            h["is_new"] = False
    json.dump(seen, open("holders_seen.json", "w"), indent=2)

    # label conviction positions from manual labels / holder tags
    hlbl = {h["coldkey"]: (h["label"], h["hl"]) for h in holders if h.get("label")}
    for r in conviction.get("top", []):
        ck = r["coldkey"]
        if ck in MANUAL_LABELS:
            r["label"], r["hl"] = MANUAL_LABELS[ck][0], MANUAL_LABELS[ck][1]
        else:
            r["label"], r["hl"] = hlbl.get(ck, (None, None))

    validators = [{"name": v.get("name") or "Unnamed", "hotkey": v["hotkey"],
                   "stake_alpha": f(v.get("total_hotkey_alpha", 0)) / 1e9,
                   "dominance_pct": f(v.get("stake_dominance_percent", 0)),
                   "apr_pct": f(v.get("apr_percent", 0)), "nominators": v.get("nominator_count", 0)}
                  for v in vals[:10]]

    try:
        holder_total = tao_get(token, "/subnets/%d/holders/top?limit=1" % NETUID).get("pagination", {}).get("total", 0)
    except Exception:
        holder_total = 0

    data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "tao.com Data API" + (" + taostats" if TAOSTATS_API_KEY else ""),
        "taostats_enabled": bool(TAOSTATS_API_KEY),
        "tao": {"price_usd": tao_usd, "change_24h_pct": f(tao["price_change_percent_24h"]),
                "market_cap_usd": f(tao["market_cap_usd"])},
        "network": {"total_subnets": net["total_subnets"], "staked_tao": f(net["staked_tao"]),
                    "staked_root_pct": f(net["staked_root_percent"]),
                    "staked_alpha_pct": f(net["staked_alpha_percent"])},
        "subnet": {"netuid": NETUID, "name": sub["name"], "price_usd": price_usd, "price_tao": price_tao,
                   "market_cap_usd": f(sub["market_cap_usd"]), "fdv_usd": f(sub["fully_diluted_value_usd"]),
                   "volume_24h_usd": f(sub["volume_24h_usd"]), "change_24h_pct": f(pool["price_change_percent_24h"]),
                   "change_7d_pct": f(pool["price_change_percent_1w"]), "change_30d_pct": f(pool["price_change_percent_30d"]),
                   "tao_in_pool": f(pool["tao_in_tao"]), "alpha_in_pool": f(pool["alpha_in_tao"]),
                   "circulating_alpha": circ_alpha, "total_traders": sub.get("total_traders", 0),
                   "holder_count": holder_total, "rank": leaderboard["subnet_rank"]},
        "leaderboard": leaderboard,
        "emissions": {"total": emit(emis["emissions_per_day_tao"]), "owner": emit(emis["owner_emissions_per_day_tao"]),
                      "miner": emit(emis["miner_emissions_per_day_tao"]), "validator": emit(emis["validator_emissions_per_day_tao"]),
                      "incentive_burn_pct": f(emis["incentive_burn_percent"])},
        "conviction": conviction,
        "validators": validators,
        "holders": holders,
    }

    with open("data.json", "w") as fh:
        json.dump(data, fh, indent=2)

    conv = ("conviction %.2f%% (%d locks)" % (conviction["pct_of_circulating"], conviction["lock_count"])
            if conviction.get("available") else "conviction OFF")
    print("data.json @ %s | SN%d %s $%.4f | rank #%d | %d holders (%d tagged, %d val) | %s | id_map=%d" % (
        data["generated_at"], NETUID, data["subnet"]["name"], price_usd, leaderboard["subnet_rank"],
        len(holders), sum(1 for h in holders if h["label"]), sum(1 for h in holders if h["is_val"]),
        conv, len(id_map)))


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print("HTTP %s: %s" % (e.code, e.read().decode()[:300]), file=sys.stderr)
        sys.exit(1)
