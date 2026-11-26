# scm8-data

Daily NSE end-of-day prices (official NSE bhavcopy) for the SCM-8 small-cap momentum pilot.

- `data/daily/<symbol>.csv` — Date, Close, Volume for NSE EQ/BE/BZ stocks, last ~1100 trading days, split/bonus adjusted.
- `data/corp_actions.csv` — bonus/split list (NSE corporate-actions API, backup: `bc` file in NSE's PR zip). Factors are applied on the ex-date only when the day's price gap confirms them. NSE's bhavcopy 'previous close' is NOT adjusted, so it is not used for this (fixed 15 Nov 2026).
- `data/status.json` also lists `unexplained_gaps` (1.8x+ one-day moves with no listed action, not adjusted) and `actions_not_confirmed`.
- 15 Nov 2026: 43 stocks' history repaired from clean NSE data (see `data/repair_2026-11-15.json`).
- `data/isin_symbol_map.json`, `data/meta.json` (NSE holidays), `data/status.json` (last update).
- Updated by GitHub Actions (`.github/workflows/daily.yml`) at about 18:30 IST on weekdays, retry 19:20 IST.
- History up to 13 Oct 2026 bootstrapped from [eod2_data](https://github.com/BennyThadikaran/eod2_data).

- `pe/pe_history.csv` — Nifty 50 / Midcap 50 / Smallcap 50 P/E (niftyindices.com), updated by `.github/workflows/pe.yml` after each price run; `pe/status.json` shows the last date and any download problem.

Only public market data lives here — no holdings or account information.
Each January, add the new year's NSE holidays to `data/meta.json`.
