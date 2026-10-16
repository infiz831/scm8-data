# scm8-data

Daily NSE end-of-day prices (official NSE bhavcopy) for the SCM-8 small-cap momentum pilot.

- `data/daily/<symbol>.csv` — Date, Close, Volume for NSE EQ/BE/BZ stocks, last ~1100 trading days, split/bonus adjusted.
- `data/isin_symbol_map.json`, `data/meta.json` (NSE holidays), `data/status.json` (last update).
- Updated by GitHub Actions (`.github/workflows/daily.yml`) at about 18:30 IST on weekdays, retry 19:20 IST.
- History up to 13 Oct 2026 bootstrapped from [eod2_data](https://github.com/BennyThadikaran/eod2_data).

Only public market data lives here — no holdings or account information.
Each January, add the new year's NSE holidays to `data/meta.json`.
