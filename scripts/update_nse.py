"""Daily NSE end-of-day updater for the SCM-8 pilot (also used by the PC backup).

Downloads the official NSE cash-market bhavcopy (UDiFF format) for every missing trading day and
appends Date,Close,Volume to data/daily/<symbol>.csv (series EQ, BE, BZ).

SPLITS / BONUSES (changed 15 Nov 2026): NSE's bhavcopy does NOT adjust 'previous close' (PrvsClsgPric) on
ex-dates, so it cannot be used to detect corporate actions. Factors now come from:
  1. NSE's corporate-actions list (www.nseindia.com/api/corporates-corporateActions), refreshed every run;
  2. as a backup, the 'bc' (book-closure) file inside NSE's daily PR zip (nsearchives), ex-date = record date.
Known actions are kept in data/corp_actions.csv. On the ex-date, history is divided by the factor (bonus a:b
-> (a+b)/b, split Rs X to Rs Y -> X/Y) before the new day is appended - only if today's price gap confirms it.
A one-day move of 1.8x+ with no listed action is NOT adjusted but reported in status.json 'unexplained_gaps'.

Usage: python scripts/update_nse.py [data_dir]
Writes data/status.json with what happened. Exit code 0 even when today's file is not out yet.
"""
import datetime as dt, io, json, math, os, re, sys, time, zipfile
import pandas as pd, requests

ROOT = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith('-') else 'data'
D = os.path.join(ROOT, 'daily')
KEEP = 1100                      # trading days of history kept per stock
SERIES = ('EQ', 'BE', 'BZ')      # same universe as eod2 (SME series excluded)
GAP_LOCK = 0.03                  # NSE previous close vs ours: >3% apart ...
MAX_GAP = 30                     # ... in more than this many stocks = a missing session (e.g. Muhurat): stop
CONFIRM = math.log(1.5)          # an action is applied only if the observed price gap is within 1.5x of its factor
UNEXPLAINED = 1.8                # report (not adjust) one-day moves of 1.8x+ that have no listed action
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/126.0 Safari/537.36',
      'Accept': '*/*', 'Accept-Language': 'en-US,en;q=0.9', 'Referer': 'https://www.nseindia.com/'}
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
CA_COLS = ['symbol', 'isin', 'ex_date', 'factor', 'subject', 'source', 'applied']
NUM = r'(\d+(?:\.\d+)?)'


# ---------------------------------------------------------------- corporate actions
def parse_factor(subject):
    """bonus a:b -> (a+b)/b ; split / sub-division / consolidation 'from Rs X to Rs Y' -> X/Y ; else 1"""
    s = str(subject).lower().replace(' ', '')
    f = 1.0
    if not re.search('ncrp|preference|debenture', s):
        for a, b in re.findall(r'bonus(?:-|issue)?' + NUM + r':' + NUM, s):
            if float(b) > 0:
                f *= (float(a) + float(b)) / float(b)
    m = re.search(r'(split|sub-division|subdivision|consolidation)[^0-9]*?(?:from)?(?:rs|re)\.?' + NUM
                  + r'.*?to(?:rs|re)\.?' + NUM, s)
    if m:
        x, y = float(m.group(2)), float(m.group(3))
        if x > 0 and y > 0 and x != y:
            f *= x / y
    return f


def load_ca(root):
    p = os.path.join(root, 'corp_actions.csv')
    if os.path.exists(p):
        ca = pd.read_csv(p, dtype=str).fillna('')
        return ca.reindex(columns=CA_COLS, fill_value='')
    return pd.DataFrame(columns=CA_COLS)


def save_ca(root, ca):
    ca = ca.drop_duplicates(['symbol', 'ex_date', 'factor'], keep='first').sort_values(['ex_date', 'symbol'])
    ca.to_csv(os.path.join(root, 'corp_actions.csv'), index=False)


def add_actions(ca, rows):
    new = pd.DataFrame(rows, columns=CA_COLS[:-1]).astype(str)
    if new.empty:
        return ca, 0
    new['applied'] = ''
    key = lambda d: d['symbol'] + '|' + d['ex_date'] + '|' + d['factor']
    have = set(key(ca)) if len(ca) else set()
    new = new[~key(new).isin(have)]
    return pd.concat([ca, new], ignore_index=True), len(new)


def fetch_ca_api(session, start, end):
    """NSE corporate-actions list (main board) between two dates -> rows with a bonus/split factor"""
    for u in ('https://www.nseindia.com/', 'https://www.nseindia.com/companies-listing/corporate-filings-actions'):
        session.get(u, timeout=30, headers={'Accept': 'text/html'})
        time.sleep(1)
    url = ('https://www.nseindia.com/api/corporates-corporateActions?index=equities'
           f'&from_date={start:%d-%m-%Y}&to_date={end:%d-%m-%Y}')
    r = session.get(url, timeout=60, headers={'Accept': 'application/json, text/plain, */*'})
    r.raise_for_status()
    data = r.json()
    data = data if isinstance(data, list) else data.get('data', [])
    rows = []
    for a in data:
        f = parse_factor(a.get('subject', ''))
        ex = pd.to_datetime(a.get('exDate'), format='%d-%b-%Y', errors='coerce')
        if f != 1.0 and not pd.isna(ex):
            rows.append([str(a.get('symbol', '')).strip(), str(a.get('isin', '')).strip(), str(ex.date()),
                         f'{f:.6g}', str(a.get('subject', '')).strip(), 'nse_api'])
    return rows


def parse_date(x):
    """explicit formats only (pandas' dayfirst guess turns 2026-11-04 into 11 April)"""
    x = str(x).strip()
    for fmt in ('%Y-%m-%d', '%d-%b-%Y', '%d-%b-%y', '%d-%m-%Y', '%d/%m/%Y', '%d %b %Y', '%d-%B-%Y'):
        try:
            return pd.Timestamp(dt.datetime.strptime(x, fmt))
        except ValueError:
            pass
    return pd.NaT


def fetch_ca_pr(session, day):
    """backup source: 'bc' file in NSE's daily PR zip (actions announced that day; ex-date = record date)"""
    url = f'https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR{day:%d%m%y}.zip'
    r = session.get(url, timeout=40)
    if r.status_code != 200 or r.content[:2] != b'PK':
        return None
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = [n for n in z.namelist() if os.path.basename(n).lower().startswith('bc')]
    if not names:
        return []
    bc = pd.read_csv(z.open(names[0]), dtype=str)
    bc.columns = [c.strip().upper() for c in bc.columns]
    col = lambda *keys: next((c for c in bc.columns if all(k in c for k in keys)), None)
    c_sym, c_pur, c_ser = col('SYMBOL'), col('PURPOSE'), col('SERIES')
    c_ex = col('EX') if col('EX') and 'EXP' not in col('EX') else None
    c_rec, c_bcs = col('RECORD'), col('BC', 'ST')
    rows = []
    for _, t in bc.iterrows():
        if c_ser and str(t[c_ser]).strip() not in SERIES:
            continue
        f = parse_factor(t[c_pur]) if c_pur else 1.0
        if f == 1.0:
            continue
        d = next((t[c] for c in (c_ex, c_rec, c_bcs) if c and str(t[c]).strip() not in ('', '-', 'nan')), None)
        ex = parse_date(d) if d else pd.NaT
        if not pd.isna(ex):
            rows.append([str(t[c_sym]).strip(), '', str(ex.date()), f'{f:.6g}', str(t[c_pur]).strip(), 'nse_pr_bc'])
    return rows


# ---------------------------------------------------------------- download
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


# ---------------------------------------------------------------- main
def main(root=None):
    global ROOT, D
    if root:
        ROOT = root; D = os.path.join(ROOT, 'daily')
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
    status = dict(previous_last_date=str(last), adjustments=[], unexplained_gaps=[], actions_not_confirmed=[],
                  ca_sources={}, skipped_days=[], new_symbols=[], note='')
    s = requests.Session(); s.headers.update(UA)

    # 1) refresh the corporate-actions list (API first; PR 'bc' files per day as a backup)
    ca = load_ca(ROOT)
    try:
        rows = fetch_ca_api(s, last - dt.timedelta(days=20), end + dt.timedelta(days=60))
        ca, k = add_actions(ca, rows); status['ca_sources']['nse_api'] = f'ok, {len(rows)} bonus/split rows, {k} new'
    except Exception as e:
        status['ca_sources']['nse_api'] = f'failed: {str(e)[:120]}'
    pr_ok = pr_fail = 0
    d0 = last - dt.timedelta(days=10)
    while d0 <= end:
        if d0.weekday() < 5 and d0 not in hol:
            try:
                rows = fetch_ca_pr(s, d0)
                if rows is None:
                    pr_fail += 1
                else:
                    ca, _ = add_actions(ca, rows); pr_ok += 1
            except Exception:
                pr_fail += 1
            time.sleep(0.5)
        d0 += dt.timedelta(days=1)
    status['ca_sources']['nse_pr_bc'] = f'{pr_ok} days read, {pr_fail} not available'
    ca['ex_d'] = pd.to_datetime(ca['ex_date'], errors='coerce')

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
        # safety lock: NSE's (unadjusted) previous close differing from ours in many stocks at once means a
        # session is missing from our data (e.g. an unlisted Muhurat session). Save nothing.
        n_gap = 0
        for t in df.itertuples(index=False):
            fname = isin2file.get(t.ISIN)
            path = os.path.join(D, f'{fname}.csv') if fname else None
            if path and os.path.exists(path) and float(t.PrvsClsgPric) > 0:
                lc = pd.read_csv(path, usecols=['Date', 'Close']).iloc[-1]
                if str(lc['Date']) < ds and lc['Close'] > 0 and abs(float(t.PrvsClsgPric) / float(lc['Close']) - 1) > GAP_LOCK:
                    n_gap += 1
        if n_gap > MAX_GAP:
            status['note'] = (f'MASS GAP BLOCKED on {ds}: {n_gap} stocks differ >3% from our previous close - '
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
            close, vol = float(t.ClsPric), int(t.TtlTradgVol)
            vwap = float(t.TtlTrfVal) / vol if vol > 0 and float(t.TtlTrfVal) > 0 else close
            if os.path.exists(path):
                d = pd.read_csv(path)
                prev_date = str(d['Date'].iloc[-1])
                if prev_date >= ds:
                    continue
                lastc = float(d['Close'].iloc[-1])
                obs = lastc / vwap if vwap > 0 else 1.0               # observed price gap since our last close
                # corporate actions of this stock with ex-date after our last stored day, up to today
                m = ((ca['isin'] == isin) | (ca['symbol'] == sym)) & (ca['applied'] == '') & \
                    (ca['ex_d'] > pd.Timestamp(prev_date)) & (ca['ex_d'] <= pd.Timestamp(ds))
                if m.any():
                    f = float(pd.to_numeric(ca.loc[m, 'factor']).drop_duplicates().prod())
                    if abs(math.log(obs / f)) < CONFIRM:
                        d['Close'] = d['Close'] / f
                        d['Volume'] = (d['Volume'] * f).round().astype('int64')
                        ca.loc[m, 'applied'] = ds
                        status['adjustments'].append(dict(symbol=sym, date=ds, factor=round(f, 5),
                                                          subject='; '.join(ca.loc[m, 'subject'].unique())))
                    else:
                        status['actions_not_confirmed'].append(dict(symbol=sym, date=ds, factor=round(f, 5),
                                                                    observed=round(obs, 3)))
                elif lastc > 0 and (obs >= UNEXPLAINED or obs <= 1 / UNEXPLAINED):
                    status['unexplained_gaps'].append(dict(symbol=sym, date=ds, last_close=lastc, close=close,
                                                           ratio=round(obs, 3)))
                d = pd.concat([d, pd.DataFrame([dict(Date=ds, Close=close, Volume=vol)])], ignore_index=True)
            else:
                d = pd.DataFrame([dict(Date=ds, Close=close, Volume=vol)])
            d.tail(KEEP).to_csv(path, index=False, float_format='%.2f')
        added.append(ds)
        day += dt.timedelta(days=1)

    save_ca(ROOT, ca.drop(columns=['ex_d']))
    if added:
        idx = pd.concat([dates.to_frame(), pd.DataFrame({'Date': added})], ignore_index=True).drop_duplicates()
        idx.tail(KEEP).to_csv(idx_path, index=False)
        json.dump({'sym2isin': sym2isin}, open(mp_path, 'w'), indent=0, sort_keys=True)
    final_last = added[-1] if added else str(last)
    status.update(last_date=final_last, days_added=added, updated_at=now.isoformat(timespec='seconds'))
    json.dump(status, open(os.path.join(ROOT, 'status.json'), 'w'), indent=1)
    print(json.dumps(status, indent=1))
    return status


if __name__ == '__main__':
    main(ROOT)
