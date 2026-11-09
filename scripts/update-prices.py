#!/usr/bin/env python3
"""Fetch latest prices for the portfolio dashboard and write prices.json.

Runs in GitHub Actions (and locally for testing). Per symbol it tries the
Yahoo chart API first, then the Stooq CSV as a fallback. Values that cannot
be fetched are kept from the previous prices.json when available, otherwise
from the built-in fallback closes (same as the page's FALLBACK).

The file is only rewritten when at least one symbol was fetched live, or
when no previous prices.json exists at all. Standard library only.

prices.json format:
  {"updated_at": "ISO UTC time", "updated_at_beijing": "YYYY-MM-DD HH:MM",
   "source": "yahoo|stooq|mixed|fallback",
   "prices": {"RKLB": {"price": 53.07, "prev_close": 50.0,
                       "change_pct": 6.14, "source": "yahoo"}, ...}}
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

YAHOO_SYMBOLS = {
    'RKLB': 'RKLB', 'TSLA': 'TSLA', 'GEV': 'GEV', 'GOOGL': 'GOOGL',
    'SPCX': 'SPCX', 'BRK.B': 'BRK-B', 'NVDA': 'NVDA', 'MU': 'MU',
    'AMD': 'AMD', 'HK700': '0700.HK', 'HK9992': '9992.HK',
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


def fetch_yahoo(sym):
    url = ('https://query1.finance.yahoo.com/v8/finance/chart/%s'
           '?range=1d&interval=1d' % YAHOO_SYMBOLS[sym])
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    meta = data['chart']['result'][0]['meta']
    price = meta.get('regularMarketPrice')
    prev = meta.get('chartPreviousClose')
    if price is None:
        return None
    entry = {'price': round(float(price), 2),
             'prev_close': round(float(prev), 2) if prev is not None else None,
             'change_pct': None}
    if prev:
        entry['change_pct'] = round((float(price) - float(prev)) / float(prev) * 100, 2)
    return entry


def fetch_stooq(sym):
    url = 'https://stooq.com/q/l/?s=%s&f=sd2t2ohlcv&h&e=csv' % STOOQ_SYMBOLS[sym]
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        text = r.read().decode('utf-8', 'replace')
    lines = [ln for ln in text.replace('\r', '').split('\n') if ln.strip()]
    if len(lines) < 2:
        return None
    fields = lines[1].split(',')
    if len(fields) < 7:
        return None
    close = float(fields[6])
    return {'price': round(close, 2), 'prev_close': None, 'change_pct': None}


def main():
    prices_path = os.environ.get('PRICES_JSON')
    if not prices_path:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        prices_path = os.path.join(repo_root, 'prices.json')
    old = {}
    if os.path.exists(prices_path):
        try:
            with open(prices_path, encoding='utf-8') as f:
                old = json.load(f).get('prices', {}) or {}
        except Exception as e:
            print('cannot read old prices.json:', e)
            old = {}

    out = {}
    live_sources = []
    for sym in YAHOO_SYMBOLS:
        entry = None
        src = None
        try:
            entry = fetch_yahoo(sym)
            if entry:
                src = 'yahoo'
        except Exception as e:
            print('yahoo fail', sym, e)
        if not entry:
            try:
                entry = fetch_stooq(sym)
                if entry:
                    src = 'stooq'
            except Exception as e:
                print('stooq fail', sym, e)
        if entry:
            if src == 'stooq' and entry.get('prev_close') is None:
                # Stooq CSV has no previous close; reuse the previous
                # file's prev_close when available.
                prev = (old.get(sym) or {}).get('prev_close')
                if prev:
                    entry['prev_close'] = prev
                    entry['change_pct'] = round(
                        (entry['price'] - prev) / prev * 100, 2)
            entry['source'] = src
            out[sym] = entry
            live_sources.append(src)
        elif old.get(sym):
            old_entry = dict(old[sym])
            old_entry['source'] = 'stale'
            out[sym] = old_entry
        else:
            out[sym] = {'price': FALLBACK[sym], 'prev_close': None,
                        'change_pct': None, 'source': 'fallback'}

    live = len(live_sources)
    if live == 0 and old:
        print('no live prices fetched; keeping existing prices.json unchanged')
        return 0

    if live == len(YAHOO_SYMBOLS) and set(live_sources) == {'yahoo'}:
        file_source = 'yahoo'
    elif live == len(YAHOO_SYMBOLS) and set(live_sources) == {'stooq'}:
        file_source = 'stooq'
    elif live == 0:
        file_source = 'fallback'
    else:
        file_source = 'mixed'

    now = datetime.now(timezone.utc)
    bj = now + timedelta(hours=8)
    payload = {
        'updated_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'updated_at_beijing': bj.strftime('%Y-%m-%d %H:%M'),
        'source': file_source,
        'prices': out,
    }
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
        print('  %-7s %-8s price=%s prev=%s chg=%s' % (
            sym, e.get('source'), e.get('price'), e.get('prev_close'),
            e.get('change_pct')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
