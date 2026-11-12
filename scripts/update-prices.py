#!/usr/bin/env python3
"""Fetch latest prices for the portfolio dashboard and write prices.json.

Runs in GitHub Actions (and locally for testing). Per symbol it walks a
fallback chain:

    Yahoo chart API (query1, then query2; 2 attempts each with backoff)
      -> Tencent qt.gtimg.cn (no key, GBK text)
      -> Sina hq.sinajs.cn (needs a Referer header, GBK text)
      -> Stooq CSV

Values that cannot be fetched from any source are kept from the previous
prices.json when available, otherwise from the built-in fallback closes
(same as the page's FALLBACK).

The file is only rewritten when at least one symbol was fetched live, or
when no previous prices.json exists at all. Standard library only.

prices.json format:
  {"updated_at": "ISO UTC time", "updated_at_beijing": "YYYY-MM-DD HH:MM",
   "source": "yahoo|tencent|sina|stooq|mixed|fallback",
   "prices": {"RKLB": {"price": 53.07, "prev_close": 50.0,
                       "change_pct": 6.14, "source": "yahoo",
                       "ext_session": "pre", "ext_change_pct": 0.43}, ...}}

Extended-hours fields (added 2026-11-09 for the old static board):
  "ext_session": "pre" | "post" | null (US Eastern 04:00-09:30 pre,
  16:00-20:00 post; null in regular hours and after close),
  "ext_change_pct": pre/post change % vs prev close, 2 decimals, or null.
  Yahoo supplies preMarketChangePercent/postMarketChangePercent in the
  quote API, but the chart meta used here does not carry them (probed
  2026-11-09); fulldayChangePercent is used instead -- during pre/post
  it equals the ext change vs the last regular close. Sina US carries
  the ext change% in field [22]. Tencent and Stooq supply no ext data.
  HK symbols always have null ext fields (no US sessions).

Field layouts (verified 2026-11-09 against live responses):
  Tencent US  v_usSYM="200~name~CODE~price~prev~open~...~change~chg%~high~low~USD~..."
              price=[3], prev=[4], change_pct=[31]
  Tencent HK  v_hkNNNNN="100~name~CODE~price~prev~open~...~change~chg%~high~low~...~HKD~..."
              price=[3], prev=[4], change_pct=[30]
  Sina US     var hq_str_gb_sym="name,price,chg%,datetime,change,open,high,low,...,
              prev_close(at [26]),..."
              price=[1], prev=[26], change_pct=[2]
  Sina HK     var hq_str_hkNNNNN="EN,CN,open,prev,high,low,price,change,chg%,..."
              price=[6], prev=[3], change_pct=[8]
  Sina has no usable BRK.B quote (gb_brk.b is empty, gb_brkb is a stale
  2019 record), so BRK.B skips Sina.
  Sina US ext fields (verified live 2026-11-09 in pre-market): [21] ext
  price, [22] ext change%, [23] ext change, [24] ext quote time
  ('Nov 09 07:37AM EST'); [1] is only the last regular close, so the
  ext value must come from [22], never (price-prev)/prev.
  Tencent US quotes do NOT update in pre/post sessions ([3] is the last
  regular close, timestamped last close), so Tencent supplies no ext
  data -- computing (price-prev)/prev would mislabel Friday's change
  as the pre-market change. Same for Stooq (close only).

Day P&L recording (added 2026-11-12, US close auto-record):
  after the US close (Eastern weekday 16:05 or later) the script also
  computes the day's P&L from the resolved closes and the share counts
  in holdings.json, and appends it to prices.json under "pnl":
  {"daily": [{"d": "2026-11-12", "pnl": 1234.56, "pct": 1.23}, ...]}.
  Only one record per trading day, never overwritten; a day is recorded
  only when every holding has a live close price (no stale/fallback
  entries). The dashboard page reads data.pnl.daily and merges it into
  its P&L calendar. The workflow runs every 15 minutes during US hours,
  so the first run after 16:05 ET writes the record.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

YAHOO_SYMBOLS = {
    'RKLB': 'RKLB', 'TSLA': 'TSLA', 'GEV': 'GEV', 'GOOGL': 'GOOGL',
    'SPCX': 'SPCX', 'BRK.B': 'BRK-B', 'NVDA': 'NVDA', 'MU': 'MU',
    'AMD': 'AMD', 'HK700': '0700.HK', 'HK9992': '9992.HK',
}
TENCENT_SYMBOLS = {
    'RKLB': 'usRKLB', 'TSLA': 'usTSLA', 'GEV': 'usGEV', 'GOOGL': 'usGOOGL',
    'SPCX': 'usSPCX', 'BRK.B': 'usBRK.B', 'NVDA': 'usNVDA', 'MU': 'usMU',
    'AMD': 'usAMD', 'HK700': 'hk00700', 'HK9992': 'hk09992',
}
SINA_SYMBOLS = {
    'RKLB': 'gb_rklb', 'TSLA': 'gb_tsla', 'GEV': 'gb_gev', 'GOOGL': 'gb_googl',
    'SPCX': 'gb_spcx', 'BRK.B': None, 'NVDA': 'gb_nvda', 'MU': 'gb_mu',
    'AMD': 'gb_amd', 'HK700': 'hk00700', 'HK9992': 'hk09992',
}
STOOQ_SYMBOLS = {
    'RKLB': 'rklb.us', 'TSLA': 'tsla.us', 'GEV': 'gev.us', 'GOOGL': 'googl.us',
    'SPCX': 'spcx.us', 'BRK.B': 'brk-b.us', 'NVDA': 'nvda.us', 'MU': 'mu.us',
    'AMD': 'amd.us', 'HK700': '0700.hk', 'HK9992': '9992.hk',
}
# Built-in fallback closes (same as the page's FALLBACK). Only used when
# there is no previous prices.json and nothing could be fetched.
FALLBACK = {
    'RKLB': 53.07, 'TSLA': 358.22, 'GEV': 774.11, 'GOOGL': 284.78,
    'SPCX': 134.27, 'BRK.B': 570.89, 'NVDA': 186.64, 'MU': 690.00,
    'AMD': 449.04, 'HK700': 419.80, 'HK9992': 141.90,
}
UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/120.0 Safari/537.36'}
SINA_HEADERS = dict(UA, Referer='https://finance.sina.com.cn/')

# US-listed symbols have pre/post sessions; HK symbols do not.
US_SYMBOLS = {s for s in YAHOO_SYMBOLS if s not in ('HK700', 'HK9992')}


def _ext_session(now=None):
    """Current US extended-hours session ('pre'/'post') by Eastern time.

    Pre-market 04:00-09:30, regular 09:30-16:00, post-market 16:00-20:00
    Eastern; anything else is closed (None).
    """
    try:
        from zoneinfo import ZoneInfo
        eastern = (now or datetime.now(timezone.utc)).astimezone(
            ZoneInfo('America/New_York'))
    except Exception:
        eastern = (now or datetime.now(timezone.utc)) + timedelta(hours=-5)
    minutes = eastern.hour * 60 + eastern.minute
    if 4 * 60 <= minutes < 9 * 60 + 30:
        return 'pre'
    if 16 * 60 <= minutes < 20 * 60:
        return 'post'
    return None


def _et_now(now=None):
    """Current time in America/New_York (aware datetime)."""
    try:
        from zoneinfo import ZoneInfo
        return (now or datetime.now(timezone.utc)).astimezone(
            ZoneInfo('America/New_York'))
    except Exception:
        return (now or datetime.now(timezone.utc)) + timedelta(hours=-5)


def _read_holdings(repo_root):
    """Share counts per symbol from holdings.json (repo root).

    Returns {sym: shares} for stock holdings, or {} when unreadable.
    Only the share counts matter for the day P&L; cash has none.
    """
    path = os.path.join(repo_root, 'holdings.json')
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        out = {}
        for h in data.get('holdings', []) or []:
            sym = h.get('sym')
            shares = h.get('shares')
            if isinstance(sym, str) and isinstance(shares, (int, float)) and shares:
                out[sym] = shares
        return out
    except Exception as e:
        print('cannot read holdings.json:', e)
        return {}


def _day_pnl_record(out, holdings, date_str):
    """Build one close-based day P&L record from resolved prices.

    Returns {'d': date_str, 'pnl': float, 'pct': float} with pct in
    percent units, or None when the day cannot be recorded (a holding
    is missing, or its price is not a live close, or a prev close is
    missing). Must only be called after the close: after 16:00 ET
    every source's price is the regular close (Tencent/Sina stop
    updating at the close; Yahoo's regular price freezes at the
    close), so (price - prev_close) * shares is the day's return.
    """
    if not holdings:
        return None
    total_pnl = 0.0
    total_prev = 0.0
    for sym, shares in holdings.items():
        e = out.get(sym)
        if not e:
            return None
        if e.get('source') in ('stale', 'fallback'):
            return None
        price = e.get('price')
        prev = e.get('prev_close')
        if price is None or prev is None:
            return None
        total_pnl += (price - prev) * shares
        total_prev += prev * shares
    if total_prev <= 0:
        return None
    return {'d': date_str,
            'pnl': round(total_pnl, 2),
            'pct': round(total_pnl / total_prev * 100, 2)}


def _sina_ext(f):
    """Sina US fields [21]/[22]/[23] are the extended-hours price,
    change% and change (pre-market in the morning, post-market in the
    evening); [24] is the ext quote time. Return the change% or None."""
    try:
        return round(float(f[22]), 2)
    except (TypeError, ValueError, IndexError):
        return None


def _validate(price, prev, chg):
    """Check a parsed quote for sanity; return a normalized entry or None.

    Rejects non-positive prices and quotes whose reported change% disagrees
    with (price - prev) / prev, which catches wrong field layouts.
    """
    try:
        price = float(price)
        prev = float(prev) if prev not in (None, '') else None
        chg = float(chg) if chg not in (None, '') else None
    except (TypeError, ValueError):
        return None
    if not price or price <= 0:
        return None
    if prev is not None and prev <= 0:
        prev = None
    if prev is not None and chg is not None:
        implied = (price - prev) / prev * 100
        if abs(chg - implied) > 1.0:
            return None
    if prev is not None and chg is None:
        chg = round((price - prev) / prev * 100, 2)
    return {'price': round(price, 2),
            'prev_close': round(prev, 2) if prev is not None else None,
            'change_pct': chg}


def _get(url, headers=None, timeout=12):
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_yahoo(sym, session=None):
    """Yahoo chart API: query1 then query2, 2 attempts each, backoff 1s/3s.

    In pre/post sessions, also read preMarketChangePercent /
    postMarketChangePercent from meta into entry['ext_change_pct'].
    """
    for host in ('query1', 'query2'):
        for attempt in range(2):
            try:
                url = ('https://%s.finance.yahoo.com/v8/finance/chart/%s'
                       '?range=1d&interval=1d' % (host, YAHOO_SYMBOLS[sym]))
                data = json.loads(_get(url))
                meta = data['chart']['result'][0]['meta']
                entry = _validate(meta.get('regularMarketPrice'),
                                  meta.get('chartPreviousClose'), None)
                if entry:
                    if session and sym in US_SYMBOLS:
                        # Chart meta carries no preMarketChangePercent /
                        # postMarketChangePercent fields (probed 2026-11-09,
                        # incl. includePrePost=true): fulldayChangePercent
                        # is the working equivalent -- during pre/post it is
                        # the ext change vs the last regular close (RKLB
                        # +1.489% at 07:35 ET, fulldayPrice 53.86 vs
                        # regular close 53.07; same field verified against
                        # Nasdaq's pre-market % on the Muse side).
                        key = ('preMarketChangePercent' if session == 'pre'
                               else 'postMarketChangePercent')
                        ext = meta.get(key)
                        if ext is None:
                            ext = meta.get('fulldayChangePercent')
                        try:
                            entry['ext_change_pct'] = (
                                round(float(ext), 2)
                                if ext is not None else None)
                        except (TypeError, ValueError):
                            entry['ext_change_pct'] = None
                    return entry
                return None
            except Exception as e:
                print('yahoo fail', sym, host, 'attempt', attempt + 1, e)
                if attempt == 0:
                    time.sleep(1 if host == 'query1' else 3)
    return None


def fetch_tencent(sym, session=None):
    """Tencent qt.gtimg.cn: US price=[3] prev=[4] chg%=[31];
    HK price=[3] prev=[4] chg%=[30]. GBK-encoded text."""
    code = TENCENT_SYMBOLS[sym]
    is_hk = code.startswith('hk')
    for scheme in ('https', 'http'):
        try:
            raw = _get('%s://qt.gtimg.cn/q=%s' % (scheme, code), timeout=10)
            text = raw.decode('gbk', 'replace')
            m = re.search(r'v_%s="([^"]*)"' % re.escape(code), text)
            if not m:
                continue
            f = m.group(1).split('~')
            if len(f) < 35:
                continue
            entry = _validate(f[3], f[4], f[31] if not is_hk else f[30])
            return entry
        except Exception as e:
            print('tencent fail', sym, scheme, e)
    return None


def fetch_sina(sym, session=None):
    """Sina hq.sinajs.cn (Referer required): US price=[1] prev=[26]
    chg%=[2]; HK price=[6] prev=[3] chg%=[8]. GBK-encoded text."""
    code = SINA_SYMBOLS[sym]
    if code is None:
        return None
    is_hk = code.startswith('hk')
    try:
        raw = _get('https://hq.sinajs.cn/list=%s' % code,
                   headers=SINA_HEADERS, timeout=10)
        text = raw.decode('gbk', 'replace')
        m = re.search(r'var hq_str_%s="([^"]*)"' % re.escape(code), text)
        if not m:
            return None
        f = m.group(1).split(',')
        if is_hk:
            if len(f) < 9:
                return None
            entry = _validate(f[6], f[3], f[8])
        else:
            if len(f) < 27:
                return None
            entry = _validate(f[1], f[26], f[2])
            if entry and session:
                # US fields [21]/[22]/[23] are the ext price/change%/
                # change; [1] is only the last regular close, so the ext
                # value must come from [22], never (price-prev)/prev.
                entry['ext_change_pct'] = _sina_ext(f)
        return entry
    except Exception as e:
        print('sina fail', sym, e)
        return None


def fetch_stooq(sym, session=None):
    """Stooq CSV: close only, no prev close and no ext data."""
    url = 'https://stooq.com/q/l/?s=%s&f=sd2t2ohlcv&h&e=csv' % STOOQ_SYMBOLS[sym]
    try:
        text = _get(url, timeout=10).decode('utf-8', 'replace')
    except Exception as e:
        print('stooq fail', sym, e)
        return None
    lines = [ln for ln in text.replace('\r', '').split('\n') if ln.strip()]
    if len(lines) < 2:
        return None
    fields = lines[1].split(',')
    if len(fields) < 7:
        return None
    try:
        return _validate(fields[6], None, None)
    except Exception:
        return None


FETCHERS = (
    ('yahoo', fetch_yahoo),
    ('tencent', fetch_tencent),
    ('sina', fetch_sina),
    ('stooq', fetch_stooq),
)


def main():
    prices_path = os.environ.get('PRICES_JSON')
    if not prices_path:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        prices_path = os.path.join(repo_root, 'prices.json')
    old = {}
    old_pnl_daily = []
    if os.path.exists(prices_path):
        try:
            with open(prices_path, encoding='utf-8') as f:
                old_payload = json.load(f)
            old = old_payload.get('prices', {}) or {}
            old_pnl_daily = (old_payload.get('pnl') or {}).get('daily') or []
            if not isinstance(old_pnl_daily, list):
                old_pnl_daily = []
        except Exception as e:
            print('cannot read old prices.json:', e)
            old = {}
            old_pnl_daily = []

    out = {}
    live_sources = []
    session = _ext_session()
    print('ext session:', session)
    for sym in YAHOO_SYMBOLS:
        entry = None
        src = None
        for name, fn in FETCHERS:
            try:
                entry = fn(sym, session)
            except Exception as e:
                print(name, 'fail', sym, e)
                entry = None
            if entry:
                src = name
                break
        if entry:
            if entry.get('prev_close') is None:
                # Stooq CSV has no previous close; reuse the previous
                # file's prev_close when available.
                prev = (old.get(sym) or {}).get('prev_close')
                if prev:
                    entry['prev_close'] = prev
                    entry['change_pct'] = round(
                        (entry['price'] - prev) / prev * 100, 2)
            entry['source'] = src
            entry['ext_session'] = session if sym in US_SYMBOLS else None
            if 'ext_change_pct' not in entry:
                entry['ext_change_pct'] = None
            out[sym] = entry
            live_sources.append(src)
        elif old.get(sym):
            old_entry = dict(old[sym])
            old_entry['source'] = 'stale'
            out[sym] = old_entry
        else:
            out[sym] = {'price': FALLBACK[sym], 'prev_close': None,
                        'change_pct': None, 'source': 'fallback',
                        'ext_session': None, 'ext_change_pct': None}
        # Small gap between symbols so a burst of requests does not
        # trip per-second rate limits.
        time.sleep(0.5)

    live = len(live_sources)
    if live == 0 and old:
        print('no live prices fetched; keeping existing prices.json unchanged')
        return 0

    if live == 0:
        file_source = 'fallback'
    elif live == len(YAHOO_SYMBOLS) and len(set(live_sources)) == 1:
        file_source = live_sources[0]
    else:
        file_source = 'mixed'

    now = datetime.now(timezone.utc)
    bj = now + timedelta(hours=8)

    # ---- 收盘后登记当日收益（美股收盘时间点自动记）----
    # 美东工作日 16:05 之后才写；每天只记一次，永不覆盖。
    pnl_daily = list(old_pnl_daily)
    et = _et_now(now)
    if et.weekday() < 5 and (et.hour, et.minute) >= (16, 5):
        rec = _day_pnl_record(
            out, _read_holdings(os.path.dirname(prices_path)),
            et.strftime('%Y-%m-%d'))
        if rec and not any(r.get('d') == rec['d'] for r in pnl_daily):
            pnl_daily.append(rec)
            pnl_daily.sort(key=lambda r: r.get('d', ''))
            print('recorded day pnl:', rec)

    payload = {
        'updated_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'updated_at_beijing': bj.strftime('%Y-%m-%d %H:%M'),
        'source': file_source,
        'prices': out,
    }
    if pnl_daily:
        payload['pnl'] = {'daily': pnl_daily}
    new_text = json.dumps(payload, indent=2, ensure_ascii=False) + '\n'
    old_text = None
    if os.path.exists(prices_path):
        with open(prices_path, encoding='utf-8') as f:
            old_text = f.read()
    if new_text != old_text:
        with open(prices_path, 'w', encoding='utf-8') as f:
            f.write(new_text)
        print('wrote %s (live=%d/%d source=%s)'
              % (prices_path, live, len(YAHOO_SYMBOLS), file_source))
    else:
        print('prices.json unchanged')
    for sym in sorted(out):
        e = out[sym]
        print('  %-7s %-8s price=%s prev=%s chg=%s ext=%s/%s' % (
            sym, e.get('source'), e.get('price'), e.get('prev_close'),
            e.get('change_pct'), e.get('ext_session'),
            e.get('ext_change_pct')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
