"""Fetch daily prices for the fund tracker and write tracker/data.js.
Runs on GitHub Actions (open internet). Never raises on a single source failing: it logs and moves on,
so the page always has whatever data was obtainable. Sources: Yahoo Finance (ETFs, FX, underlying funds),
fundsingapore.com / api.newwealth.cloud (AIA ILP funds), markets.ft.com as fallback for funds."""
import json, re, sys, time, datetime, urllib.request, urllib.parse, html
from concurrent.futures import ThreadPoolExecutor

OUT = "tracker/data.js"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept": "application/json,text/html,*/*", "Accept-Language": "en-SG,en;q=0.9"}
LOG = []
def log(*a):
    s = " ".join(str(x) for x in a); print(s, flush=True); LOG.append(s)

def get(url, headers=None, retries=2, timeout=15):
    h = dict(UA); h.update(headers or {})
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            log(f"  retry {i+1} {url[:90]} -> {str(e)[:60]}"); time.sleep(1)
    return None

def to_date(ts): return datetime.datetime.utcfromtimestamp(ts).date().isoformat()

# ---------- Yahoo ----------
def yahoo_chart(sym, start="2000-01-01"):
    p1 = int(datetime.datetime.strptime(start, "%Y-%m-%d").timestamp()); p2 = int(time.time()) + 86400
    for host in ("query1", "query2"):
        txt = get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?period1={p1}&period2={p2}&interval=1d&events=div")
        if not txt: continue
        try:
            r = json.loads(txt)["chart"]["result"][0]
            ts = r["timestamp"]; q = r["indicators"]
            adj = (q.get("adjclose") or [{}])[0].get("adjclose"); close = q["quote"][0]["close"]
            ser = adj if adj and any(v is not None for v in adj) else close
            out = [[to_date(t), round(v, 6)] for t, v in zip(ts, ser) if v is not None]
            return out, r["meta"].get("currency")
        except Exception as e:
            log(f"  yahoo parse {sym}: {e}")
    return None, None

def yahoo_search(q, types=("MUTUALFUND", "ETF")):
    txt = get(f"https://query2.finance.yahoo.com/v1/finance/search?q={urllib.parse.quote(q)}&quotesCount=15&newsCount=0")
    if not txt: return []
    try: return [x for x in json.loads(txt).get("quotes", []) if x.get("quoteType") in types]
    except Exception: return []

# ---------- FX ----------
def fx_series(sym):
    s, _ = yahoo_chart(sym, "2000-01-01"); return {d: v for d, v in (s or [])}

def to_sgd(series, ccy, usd, gbp, scale=1.0):
    fx = {"USD": usd, "GBP": gbp, "GBp": gbp, "SGD": None}.get(ccy, None)
    if ccy == "GBp": scale *= 0.01
    if ccy == "SGD" or fx is None and ccy == "SGD": return [[d, round(v*scale, 4)] for d, v in series]
    if fx is None: log(f"  no FX for {ccy}, keeping native"); return [[d, round(v*scale, 4)] for d, v in series]
    out = []; last = None
    for d, v in series:
        if d in fx: last = fx[d]
        if last is None: continue
        out.append([d, round(v*scale*last, 4)])
    return out

# ---------- AIA funds via fundsingapore ----------
def fundsg_history(secid):
    """Try the known API shapes; return [[date, nav]] in SGD or None."""
    cands = [
        f"https://api.newwealth.cloud/api/v2/funds/imas_fund_sg/{secid}/price-history",
        f"https://api.newwealth.cloud/api/v2/funds/imas_fund_sg/{secid}/prices",
        f"https://api.newwealth.cloud/api/v2/funds/imas_fund_sg/{secid}/nav",
        f"https://api.newwealth.cloud/api/v2/funds/imas_fund_sg/{secid}/performance/history",
        f"https://api.newwealth.cloud/api/v2/funds/imas_fund_sg/{secid}",
        f"https://api.newwealth.cloud/api/v2/imas_fund_sg/funds/{secid}/price-history",
        f"https://api.newwealth.cloud/api/v2/imas_fund_sg/price-history?secId={secid}",
    ]
    hdr = {"Origin": "https://www.fundsingapore.com", "Referer": "https://www.fundsingapore.com/"}
    for u in cands:
        txt = get(u, hdr, retries=1, timeout=12)
        if not txt: continue
        log(f"  fundsg {u.split('imas_fund_sg')[1][:40]} -> {len(txt)} bytes: {txt[:160].replace(chr(10),' ')}")
        ser = extract_series(txt)
        if ser and len(ser) > 50: return ser
    # fund detail page (Next.js) may embed the series
    for u in (f"https://www.fundsingapore.com/fund-library/{secid}", f"https://www.fundsingapore.com/fund/{secid}", f"https://www.fundsingapore.com/funds/{secid}"):
        txt = get(u, retries=1, timeout=15)
        if txt:
            log(f"  fundsg page {u[-40:]} -> {len(txt)} bytes")
            ser = extract_series(txt.replace('\\"', '"'))
            if ser and len(ser) > 50: return ser
    return None

def extract_series(txt):
    """Find a list of {date, value} like objects anywhere in JSON/HTML text."""
    try:
        j = json.loads(txt)
        found = walk(j)
        if found: return found
    except Exception:
        pass
    m = re.findall(r'\[\s*\{[^\[\]]{0,200}?"(?:date|Date|d|x|EndDate|asOfDate)"\s*:\s*"?(\d{4}-\d{2}-\d{2})[^\[\]]*?\}\s*(?:,\s*\{[^\[\]]*?\}\s*)*\]', txt)
    for blob in re.finditer(r'\[\s*\{(?:[^\[\]]|\[[^\[\]]*\])*\}\s*\]', txt):
        s = blob.group(0)
        if '"20' in s and len(s) > 2000:
            try:
                found = walk(json.loads(s))
                if found: return found
            except Exception: continue
    return None

def walk(j, depth=0):
    if depth > 6: return None
    if isinstance(j, list) and len(j) > 50 and isinstance(j[0], dict):
        keys = j[0].keys()
        dk = next((k for k in keys if k.lower() in ("date", "d", "x", "enddate", "asofdate", "pricedate", "navdate")), None)
        vk = next((k for k in keys if k.lower() in ("value", "v", "y", "nav", "price", "close", "bid", "bidprice", "unitprice")), None)
        if dk and vk:
            out = []
            for o in j:
                try:
                    d = str(o[dk])[:10]; v = float(o[vk])
                    if re.match(r"\d{4}-\d{2}-\d{2}", d): out.append([d, v])
                except Exception: continue
            if len(out) > 50: return sorted(out)
    if isinstance(j, list) and len(j) > 50 and isinstance(j[0], (list, tuple)) and len(j[0]) >= 2:
        out = []
        for o in j:
            try:
                d = o[0]; v = float(o[1])
                if isinstance(d, (int, float)): d = to_date(d/1000 if d > 1e11 else d)
                d = str(d)[:10]
                if re.match(r"\d{4}-\d{2}-\d{2}", d): out.append([d, v])
            except Exception: continue
        if len(out) > 50: return sorted(out)
    if isinstance(j, dict):
        for v in j.values():
            f = walk(v, depth+1)
            if f: return f
    if isinstance(j, list):
        for v in j[:20]:
            f = walk(v, depth+1)
            if f: return f
    return None

# ---------- FT fallback ----------
def ft_symbol(name):
    txt = get(f"https://markets.ft.com/data/search?query={urllib.parse.quote(name)}&assetClass=Fund")
    if not txt: return None
    m = re.search(r'tearsheet/summary\?s=([A-Z0-9:\-\.]+)', txt)
    return m.group(1) if m else None

def ft_history(symbol, years=16):
    out = []
    end = datetime.date.today()
    for k in range(years):
        a = end.replace(year=end.year-1) + datetime.timedelta(days=1)
        u = f"https://markets.ft.com/data/funds/tearsheet/historical?s={symbol}&startDate={a.strftime('%Y/%m/%d')}&endDate={end.strftime('%Y/%m/%d')}"
        txt = get(u, retries=1)
        if not txt: break
        rows = re.findall(r'<span class="mod-ui-hide-small-below">([^<]+)</span></td>\s*<td>([\d,\.]+)</td>\s*<td>([\d,\.]+)</td>\s*<td>([\d,\.]+)</td>\s*<td>([\d,\.]+)</td>', txt)
        if not rows:
            rows = re.findall(r'(\w+, \w+ \d{2}, \d{4})</span></td>\s*<td>([\d,\.]+)</td>\s*<td>([\d,\.]+)</td>\s*<td>([\d,\.]+)</td>\s*<td>([\d,\.]+)</td>', txt)
        if not rows: break
        for r in rows:
            try:
                d = datetime.datetime.strptime(r[0].strip(), "%A, %B %d, %Y").date().isoformat()
                out.append([d, float(r[4].replace(",", ""))])
            except Exception: continue
        end = a - datetime.timedelta(days=1)
        time.sleep(0.3)
    out = sorted({d: v for d, v in out}.items())
    return [[d, v] for d, v in out] if len(out) > 50 else None

# ---------- main ----------
def main():
    old = {}
    try:
        old = json.loads(open(OUT).read()[len("window.TRACK="):-1])
    except Exception as e:
        log("no previous data.js", e)
    D = {"updated": datetime.date.today().isoformat(), "etf": old.get("etf", {}), "aia": old.get("aia", {}), "log": []}

    log("== FX"); usd = fx_series("SGD=X"); gbp = fx_series("GBPSGD=X"); log(f"  USD/SGD {len(usd)} GBP/SGD {len(gbp)}")

    log("== ETFs")
    ETF = {"CSPX": "CSPX.L", "IWDA": "IWDA.L", "VWRA": "VWRA.L", "EQQQ": "EQQQ.L", "VOO": "VOO", "VT": "VT", "VWO": "VWO", "EWJ": "EWJ", "XLV": "XLV"}
    def do_etf(item):
        k, sym = item; s, ccy = yahoo_chart(sym, "2000-01-01")
        if s:
            D["etf"].setdefault(k, {"name": k, "plain": "", "ter": None, "terSrc": ""})
            D["etf"][k]["series"] = to_sgd(s, ccy, usd, gbp); D["etf"][k]["src"] = f"Yahoo Finance {sym} ({ccy}), converted to SGD"
            log(f"  {k} {ccy} {len(s)} {s[0][0]}..{s[-1][0]}")
        else: log(f"  {k} FAILED")
    def do_aia(item):
        secid, f = item; ser = fundsg_history(secid)
        if not ser:
            sym = ft_symbol(f["name"]); log(f"  FT symbol for {f['name']}: {sym}")
            if sym: ser = ft_history(sym)
        if ser:
            f["series"] = ser; f["src"] = "fundsingapore.com (Morningstar) daily NAV, SGD"
            log(f"  {f['short']} {len(ser)} {ser[0][0]}..{ser[-1][0]}")
        else: log(f"  {f['short']} FAILED")
    def do_under(item):
        secid, f = item; u = f.get("underlying")
        if not u: return
        ser = None; pick = None
        for q in (u, u.replace(" Fund", "") + " A Acc USD", u.split(" (")[0]):
            for r in yahoo_search(q):
                nm = (r.get("longname") or r.get("shortname") or "").lower()
                if all(w.lower() in nm for w in u.split()[:2]): pick = r; break
            if pick: break
        if pick:
            s, ccy = yahoo_chart(pick["symbol"], "2000-01-01")
            if s and len(s) > 200:
                ser = to_sgd(s, ccy, usd, gbp); f["underlyingSeries"] = ser
                f["underlyingSrc2"] = f"Yahoo Finance {pick['symbol']} ({pick.get('longname') or pick.get('shortname')}, {ccy}), converted to SGD"
                log(f"  {f['short']} <- {pick['symbol']} {pick.get('longname')} {ccy} {len(s)} {s[0][0]}..{s[-1][0]}")
        if not ser:
            sym = ft_symbol(u); log(f"  FT symbol for {u}: {sym}")
            if sym:
                s = ft_history(sym)
                if s:
                    ccy = sym.split(":")[-1] if ":" in sym else "USD"
                    f["underlyingSeries"] = to_sgd(s, ccy, usd, gbp); f["underlyingSrc2"] = f"FT markets {sym}, converted to SGD"
                    log(f"  {f['short']} <- FT {sym} {len(s)}")
                else: log(f"  {f['short']} underlying FAILED")
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(do_etf, ETF.items()))
    log("== AIA funds (fundsingapore / FT)")
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(do_aia, list(D["aia"].items())))
    log("== Underlying funds (Yahoo search, then FT)")
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(do_under, list(D["aia"].items())))

    D["log"] = LOG[-60:]
    open(OUT, "w").write("window.TRACK=" + json.dumps(D, separators=(",", ":")) + ";")
    log(f"wrote {OUT} {len(json.dumps(D))//1024} KB")

if __name__ == "__main__":
    main()
