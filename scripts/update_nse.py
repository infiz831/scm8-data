"""Daily NSE end-of-day updater for the SCM-8 pilot.

Downloads the official NSE cash-market bhavcopy (UDiFF format) for every missing trading day,
appends Date,Close,Volume to data/daily/<symbol>.csv (series EQ, BE, BZ), and adjusts history for
splits / bonuses using NSE's own adjusted previous close (PrvsClsgPric).

Usage: python scripts/update_nse.py [data_dir]
Writes data/status.json with what happened. Exit code 0 even when today's file is not out yet.
"""
import datetime as dt, io, json, os, sys, time, zipfile
import pandas as pd, requests

ROOT = sys.argv[1] if len(sys.argv) > 1 else 'data'
D = os.path.join(ROOT, 'daily')
KEEP = 1100                      # trading days of history kept per stock
SERIES = ('EQ', 'BE', 'BZ')      # same universe as eod2 (SME series excluded)
ADJ_THRESHOLD = 0.03             # treat a >3% gap between NSE's previous close and ours as a corporate action
MAX_ADJ = 30                     # more than this many 'corporate actions' in one day = a missing session, not splits
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/126.0 Safari/537.36',
      'Accept': '*/*', 'Accept-Language': 'en-US,en;q=0.9', 'Referer': 'https://www.nseindia.com/'}
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def fetch(day, session):
    url = f'https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip'
    last = None
    for attempt in range(4):
        try:
            r = session.get(url, timeout=40)
            if r.status_code == 200 and r.content[:2] == b'PK':
                z = zipfile.ZipFile(io.BytesIO(r.content))
                return pd.read_csv(z.open(z.namelist()[0]))
            if r.status_code == 404:
                return None
            last = f'HTTP {r.status_code}'
        except Exception as e:          # network hiccup
            last = repr(e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f'download failed for {day}: {last}')


def main():
    meta = json.load(open(os.path.join(ROOT, 'meta.json')))
    hol = {dt.datetime.strptime(k, '%d-%b-%Y').date() for k in meta.get('holidays', {})}
    # special sessions on a weekend/holiday (e.g. Diwali Muhurat trading) are real sessions: fetch them
    special = {dt.datetime.strptime(k, '%d-%b-%Y').date() for k in meta.get('special_sessions', {})}
    mp_path = os.path.join(ROOT, 'isin_symbol_map.json')
    sym2isin = json.load(open(mp_path))['sym2isin']
    isin2file = {}
    for s, i in sym2isin.items():
        if os.path.exists(os.path.join(D, f'{s.lower()}.csv')):
            isin2file[i] = s.lower()
    idx_path = os.path.join(D, 'nifty 50.csv')
    dates = pd.read_csv(idx_path)['Date']
    last = pd.Timestamp(dates.iloc[-1]).date()

    now = dt.datetime.now(IST)
    end = now.date() if now.hour >= 18 else now.date() - dt.timedelta(days=1)
    status = dict(previous_last_date=str(last), adjustments=[], skipped_days=[], new_symbols=[], note='')
    s = requests.Session(); s.headers.update(UA)
    day = last + dt.timedelta(days=1)
    added = []
    while day <= end:
        if (day.weekday() >= 5 or day in hol) and day not in special:
            day += dt.timedelta(days=1); continue
        df = fetch(day, s)
        if df is None:
            if day == end:
                status['note'] = f'bhavcopy for {day} not published yet'
                break
            status['skipped_days'].append(str(day))     # unlisted holiday or NSE outage
            day += dt.timedelta(days=1); continue
        df = df[df['SctySrs'].isin(SERIES) & ~df['TckrSymb'].astype(str).str.contains('-RE')]
        ds = str(day)
        # safety lock: a gap between NSE's previous close and ours in many stocks at once means a session
        # is missing from our data (e.g. an unlisted Muhurat session), not hundreds of splits. Save nothing.
        n_gap = 0
        for t in df.itertuples(index=False):
            fname = isin2file.get(t.ISIN)
            path = os.path.join(D, f'{fname}.csv') if fname else None
            if path and os.path.exists(path) and float(t.PrvsClsgPric) > 0:
                lc = pd.read_csv(path, usecols=['Date', 'Close']).iloc[-1]
                if str(lc['Date']) < ds and lc['Close'] > 0 and abs(float(t.PrvsClsgPric) / float(lc['Close']) - 1) > ADJ_THRESHOLD:
                    n_gap += 1
        if n_gap > MAX_ADJ:
            status['note'] = (f'MASS ADJUSTMENT BLOCKED on {ds}: {n_gap} stocks differ >3% from our previous close - '
                              f'a session is probably missing (e.g. Muhurat; add it to meta.json special_sessions). '
                              f'{ds} NOT saved.')
            break
        for t in df.itertuples(index=False):
            isin, sym = t.ISIN, str(t.TckrSymb)
            if not str(isin).startswith('INE'):
                continue
            fname = isin2file.get(isin)
            if fname is None:
                fname = sym.lower(); isin2file[isin] = fname; sym2isin[sym] = isin
                status['new_symbols'].append(sym)
            path = os.path.join(D, f'{fname}.csv')
            close, prev, vol = float(t.ClsPric), float(t.PrvsClsgPric), int(t.TtlTradgVol)
            if os.path.exists(path):
                d = pd.read_csv(path)
                if str(d['Date'].iloc[-1]) >= ds:
                    continue
                lastc = float(d['Close'].iloc[-1])
                if lastc > 0 and prev > 0 and abs(prev / lastc - 1) > ADJ_THRESHOLD:
                    f = prev / lastc
                    d['Close'] = d['Close'] * f
                    d['Volume'] = (d['Volume'] / f).round().astype('int64')
                    status['adjustments'].append(dict(symbol=sym, date=ds, factor=round(f, 5)))
                d = pd.concat([d, pd.DataFrame([dict(Date=ds, Close=close, Volume=vol)])], ignore_index=True)
            else:
                d = pd.DataFrame([dict(Date=ds, Close=close, Volume=vol)])
            d.tail(KEEP).to_csv(path, index=False, float_format='%.2f')
        added.append(ds)
        day += dt.timedelta(days=1)

    if added:
        idx = pd.concat([dates.to_frame(), pd.DataFrame({'Date': added})], ignore_index=True).drop_duplicates()
        idx.tail(KEEP).to_csv(idx_path, index=False)
        json.dump({'sym2isin': sym2isin}, open(mp_path, 'w'), indent=0, sort_keys=True)
    final_last = added[-1] if added else str(last)
    status.update(last_date=final_last, days_added=added, updated_at=now.isoformat(timespec='seconds'))
    json.dump(status, open(os.path.join(ROOT, 'status.json'), 'w'), indent=1)
    print(json.dumps(status, indent=1))


if __name__ == '__main__':
    main()
