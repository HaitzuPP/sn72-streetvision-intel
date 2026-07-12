# SN72 · StreetVision by NATIX — Token Intelligence Dashboard

Four-page dashboard for Bittensor Subnet 72 (StreetVision by NATIX). Overview and wallet
pages run on the **tao.com Data API**; the trade/PnL pages run on **taostats** dTAO
delegation events.

## Files
- `index.html` — Overview: live stat band, subnet rank, emissions by key (TAO/day + USD),
  conviction & staking panel, top validators, rank neighbourhood.
- `wallets.html` — Top 25 SN72 alpha holders, tagged from taostats identity/validator/exchange
  data, with search + tracked-only filter. Addresses link to taostats.
- `analytics.html` — Portfolio summary, trade ledger and cash-flow PnL for the largest holders.
- `transactions.html` — Large trades into the pool (>$5k) and daily net inflow.
- `refresh.py` — pulls tao.com + taostats and rewrites `data.json` (overview + wallets).
- `backfill_trades.py` — pulls taostats delegation events and rewrites `trades_data.json`
  (analytics + transactions). Reads `data.json` for current holdings, so run it after `refresh.py`.
- `data.json` / `trades_data.json` — the data the pages load on open.

## How "live" works
Both APIs need a signed key and send no CORS headers, so a static page can't call them from
the browser. Instead the scripts (holding the keys server-side) write the JSON files, and the
pages `fetch('./data.json')` / `fetch('./trades_data.json')` on load. If a file is missing/blocked,
the page renders an embedded snapshot with an amber **SNAPSHOT** badge; otherwise a green **LIVE**
badge with the timestamp.

**Refresh the data:**
```bash
TAO_KEY=... TAO_SECRET=... TAOSTATS_API_KEY=... python3 refresh.py
TAOSTATS_API_KEY=... python3 backfill_trades.py
```
Serve locally so the JSON loads:
```bash
python3 -m http.server 8080   # then open http://localhost:8080/index.html
```

## Deploy (GitHub Pages)
Enable Pages on `main`. The scheduled GitHub Action (`.github/workflows/refresh.yml`) runs both
scripts every 6h and commits fresh JSON. Set repo secrets `TAO_KEY`, `TAO_SECRET`, and
`TAOSTATS_API_KEY` — never commit them.

## Notes
- **Rank** is by alpha price / FDV, matching the tao.com subnet leaderboard. SN72 shows its ±3
  rank neighbours; no pinned comps.
- **Wallet tags:** green = SN72 owner; gold ◆ = validators & named entities from taostats. Wallets
  with no on-chain identity stay untagged.
- **PnL** is cash-flow basis: proceeds (sold) + current value (held) minus invested (bought);
  rewards/emissions count as gains. `backfill_trades.py` profiles the top holders excluding the
  subnet owner and caps event history at `MAX_PAGES` pages per wallet.
- **taostats access:** the API sits behind Cloudflare, which 403s the default Python `urllib`
  User-Agent (bot-challenge 1010) — both scripts send a browser User-Agent so requests get through.
  The key is also rate-limited (~5 req/min), so taostats calls are spaced ~12s with 403/429 backoff.
  A full refresh takes a few minutes; don't run them in tight loops.
