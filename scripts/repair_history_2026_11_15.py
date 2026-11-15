"""ONE-TIME repair (15 Nov 2026). Stocks whose stored history disagreed with the clean NSE data by >3%
(splits/bonuses never adjusted since go-live, plus older splits/demergers that eod2_data missed) are
rewritten from the clean data: adjusted close, and volume scaled the same way (Close x Volume unchanged).
Source: MIC-8 clean data (official bhavcopies + NSE corporate-actions list), see Trading Strategies\\MIC-8."""
import pandas as pd, numpy as np, glob, os, sys, json
clean, root = sys.argv[1], sys.argv[2]
n = pd.read_parquet(clean, columns=['date', 'sym', 'eid', 'ac', 'q', 'cum'])
n = n[n.date >= '2022-01-01']
n['col'] = n.eid.map(n.sort_values('date').groupby('eid').sym.last().str.lower())
g = {k: v.drop_duplicates('date', keep='last').set_index('date') for k, v in n.groupby('col')}
fixed = []
for f in sorted(glob.glob(os.path.join(root, 'daily', '*.csv'))):
    s = os.path.basename(f)[:-4]
    if s.startswith('nifty') or s not in g:
        continue
    d = pd.read_csv(f, parse_dates=['Date']).set_index('Date')
    x = g[s].reindex(d.index)
    r = (d.Close / x.ac).dropna()
    if len(r) < 20:
        continue
    dev = (r / r.iloc[-1] - 1).abs()
    if dev.max() < 0.03:
        continue
    ok = x.ac.notna()
    newc = d.Close.copy(); newv = d.Volume.astype('float64').copy()
    newc[ok] = x.ac[ok]; newv[ok] = (x.q * x.cum)[ok]
    # days missing in the clean data (rare): keep stored value, rescaled to the clean basis of the nearest day
    ratio = (newc / d.Close).where(ok).bfill().ffill()
    newc[~ok] = d.Close[~ok] * ratio[~ok]; newv[~ok] = d.Volume[~ok] / ratio[~ok]
    out = pd.DataFrame({'Date': d.index.strftime('%Y-%m-%d'), 'Close': newc.values, 'Volume': newv.round().astype('int64').values})
    out.to_csv(f, index=False, float_format='%.2f')
    fixed.append(dict(symbol=s, max_diff_pct=round(float(dev.max()) * 100, 1), days_changed=int((dev > 0.03).sum())))
json.dump(fixed, open(os.path.join(root, 'repair_2026-11-15.json'), 'w'), indent=1)
print(len(fixed), 'files repaired'); print(pd.DataFrame(fixed).to_string(index=False))
