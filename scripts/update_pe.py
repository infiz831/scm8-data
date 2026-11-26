"""Daily index P/E updater (used by VRS-10). Separate from the price updater: touches only the pe/ folder.

Source: the official NSE Indices daily file ind_close_all_DDMMYYYY.csv
  1st try  https://www.niftyindices.com/Daily_Snapshot/ind_close_all_DDMMYYYY.csv
  2nd try  https://nsearchives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv
Appends Nifty 50 / Nifty Midcap 50 / Nifty Smallcap 50 P/E to pe/pe_history.csv for every missing trading day.
Writes pe/status.json. If a file cannot be fetched the stored history is left as it is and the note says why.

Usage: python scripts/update_pe.py [pe_dir] [meta.json]
"""
import datetime as dt, io, json, os, sys, time
import pandas as pd, requests

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
      'Accept': '*/*', 'Accept-Language': 'en-US,en;q=0.9'}
URLS = ['https://www.niftyindices.com/Daily_Snapshot/ind_close_all_{d:%d%m%Y}.csv',
        'https://nsearchives.nseindia.com/content/indices/ind_close_all_{d:%d%m%Y}.csv']
NAMES = {'nifty50': 'nifty 50', 'midcap50': 'nifty midcap 50', 'smallcap50': 'nifty smallcap 50'}


def fetch(day, s):
    last = None
    for u in URLS:
        url = u.format(d=day)
        for attempt in range(2):
            try:
                r = s.get(url, timeout=30)
                if r.status_code == 200 and b'Index Name' in r.content[:200]:
                    df = pd.read_csv(io.BytesIO(r.content))
                    df.columns = [c.strip() for c in df.columns]
                    name = df['Index Name'].astype(str).str.strip().str.lower()
                    row = {}
                    for k, n in NAMES.items():
                        v = df.loc[name == n, 'P/E']
                        row[k] = pd.to_numeric(v, errors='coerce').iloc[0] if len(v) else float('nan')
                    return row
                if r.status_code == 404:
                    last = '404'; break
                last = f'HTTP {r.status_code}'
            except Exception as e:
                last = repr(e)
            time.sleep(3)
    return None if last == '404' else RuntimeError(last)


def main(pe_dir='pe', meta_path='data/meta.json'):
    path = os.path.join(pe_dir, 'pe_history.csv')
    pe = pd.read_csv(path, parse_dates=['date'])
    last = pe['date'].max().date()
    hol = set()
    if meta_path and os.path.exists(meta_path):
        hol = {dt.datetime.strptime(k, '%d-%b-%Y').date() for k in json.load(open(meta_path)).get('holidays', {})}
    now = dt.datetime.now(IST)
    end = now.date() if now.hour >= 18 else now.date() - dt.timedelta(days=1)
    s = requests.Session(); s.headers.update(UA)
    added, note = [], ''
    day = last + dt.timedelta(days=1)
    while day <= end:
        if day.weekday() >= 5 or day in hol:
            day += dt.timedelta(days=1); continue
        row = fetch(day, s)
        if isinstance(row, Exception):
            note = f'P/E download failed for {day} ({row}); using the last P/E ({last}).'
            break
        if row is None:
            if day == end: note = f'Index P/E file for {day} not published yet.'
            day += dt.timedelta(days=1); continue
        added.append(dict(date=str(day), **row))
        day += dt.timedelta(days=1)
    if added:
        pe = pd.concat([pe, pd.DataFrame(added).assign(date=lambda x: pd.to_datetime(x['date']))], ignore_index=True)
        pe = pe.drop_duplicates('date', keep='last').sort_values('date')
        pe.to_csv(path, index=False, float_format='%.2f', date_format='%Y-%m-%d')
    st = dict(last_date=str(pd.to_datetime(pe['date']).max().date()), days_added=[a['date'] for a in added], note=note,
              updated_at=now.isoformat(timespec='seconds'))
    sp = os.path.join(pe_dir, 'status.json')
    old = json.load(open(sp)) if os.path.exists(sp) else {}
    if added or old.get('note') != note or old.get('last_date') != st['last_date']:    # no empty daily commits
        json.dump(st, open(sp, 'w'), indent=1)
    print(json.dumps(st, indent=1))
    return st


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'pe', sys.argv[2] if len(sys.argv) > 2 else 'data/meta.json')
