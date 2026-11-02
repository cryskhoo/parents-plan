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

def get(url, headers=None, retries=1, timeout=15):
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

# ---------- Yahoo (cookie + crumb; Yahoo 429s bare requests from cloud IPs) ----------
import http.cookiejar
CJ = http.cookiejar.CookieJar(); OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CJ)); CRUMB = None
def yget(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with OPENER.open(req, timeout=timeout) as r: return r.read().decode("utf-8", "ignore")
def yahoo_init():
    global CRUMB
    if CRUMB is not None: return CRUMB
    for attempt in range(1):
        try:
            try: yget("https://fc.yahoo.com", 15)
            except Exception: pass
            c = yget("https://query2.finance.yahoo.com/v1/test/getcrumb", 15).strip()
            if c and "<" not in c: CRUMB = c; log(f"  yahoo crumb ok ({len(c)} chars)"); return c
        except Exception as e: log(f"  yahoo crumb attempt {attempt+1}: {str(e)[:80]}"); time.sleep(3)
    CRUMB = ""; return ""

def yahoo_chart(sym, start="2000-01-01"):
    p1 = int(datetime.datetime.strptime(start, "%Y-%m-%d").timestamp()); p2 = int(time.time()) + 86400
    crumb = yahoo_init()
    for host in ("query2", "query1"):
        txt = None
        for attempt in range(1):
            try:
                txt = yget(f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?period1={p1}&period2={p2}&interval=1d&events=div&crumb={urllib.parse.quote(crumb)}"); break
            except Exception as e:
                log(f"  yahoo {sym} {host} try {attempt+1}: {str(e)[:60]}"); time.sleep(4 + 4*attempt)
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
    yahoo_init()
    try: txt = yget(f"https://query2.finance.yahoo.com/v1/finance/search?q={urllib.parse.quote(q)}&quotesCount=15&newsCount=0")
    except Exception as e: log(f"  yahoo search {q[:30]}: {str(e)[:60]}"); return []
    try: return [x for x in json.loads(txt).get("quotes", []) if x.get("quoteType") in types]
    except Exception: return []

# ---------- Stooq (CSV, no rate limit) ----------
def stooq(sym):
    txt = get(f"https://stooq.com/q/d/l/?s={sym}&i=d", retries=2, timeout=30)
    if not txt or "Date" not in txt[:50]: 
        log(f"  stooq {sym}: {(txt or '')[:80]!r}"); return None
    out = []
    for line in txt.splitlines()[1:]:
        c = line.split(",")
        if len(c) >= 5 and c[4] not in ("", "null"):
            try: out.append([c[0], float(c[4])])
            except ValueError: pass
    return out if len(out) > 50 else None

# ---------- justETF (UCITS ETFs by ISIN) ----------
def justetf(isin, ccy="USD"):
    u = f"https://www.justetf.com/api/etfs/{isin}/performance-chart?locale=en&currency={ccy}&valuesType=MARKET_VALUE&reduceData=false&includeDividends=true&features=DIVIDENDS&dateFrom=2000-01-01&dateTo={datetime.date.today().isoformat()}"
    txt = get(u, {"Referer": "https://www.justetf.com/en/etf-profile.html?isin=" + isin, "Accept": "application/json"}, retries=1, timeout=25)
    if not txt: log(f"  justetf {isin}: no response"); return None
    try:
        j = json.loads(txt); ser = j.get("series") or j.get("data") or []
        out = []
        for o in ser:
            d = o.get("date") or (o.get("x") if isinstance(o, dict) else None); v = o.get("value") if isinstance(o.get("value"), (int, float)) else (o.get("value") or {}).get("raw") if isinstance(o.get("value"), dict) else o.get("y")
            if d and v is not None: out.append([str(d)[:10], float(v)])
        log(f"  justetf {isin}: {len(out)} points; keys={list(j.keys())[:6]}; sample={txt[:160].replace(chr(10),' ')}")
        return out if len(out) > 50 else None
    except Exception as e: log(f"  justetf {isin} parse {e}: {txt[:200]!r}"); return None

# ---------- Nasdaq API (US listed ETFs) ----------
def nasdaq_hist(sym):
    u = f"https://api.nasdaq.com/api/quote/{sym}/historical?assetclass=etf&fromdate=2000-01-01&todate={datetime.date.today().isoformat()}&limit=9999"
    txt = get(u, {"Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}, retries=1, timeout=25)
    if not txt: log(f"  nasdaq {sym}: no response"); return None
    try:
        rows = json.loads(txt)["data"]["tradesTable"]["rows"]; out = []
        for r in rows:
            d = datetime.datetime.strptime(r["date"], "%m/%d/%Y").date().isoformat(); out.append([d, float(r["close"].replace("$", "").replace(",", ""))])
        out.sort(); log(f"  nasdaq {sym}: {len(out)} {out[0][0]}..{out[-1][0]}"); return out if len(out) > 50 else None
    except Exception as e: log(f"  nasdaq {sym} parse {e}: {txt[:200]!r}"); return None

# ---------- Morningstar time series (funds) ----------
def ms_series(secid, universe, ccy):
    for host, tok in (("lt.morningstar.com", "9vehuxllxs"), ("tools.morningstar.co.uk", "t92wz0sj7c"), ("tools.morningstar.co.uk", "9vehuxllxs")):
        u = f"https://{host}/api/rest.svc/timeseries_price/{tok}?id={secid}]2]0]{universe}&currencyId={ccy}&idtype=Morningstar&frequency=daily&startDate=2000-01-01&outputType=JSON"
        txt = get(u, retries=1, timeout=25)
        if not txt: continue
        try:
            j = json.loads(txt); pts = j["TimeSeries"]["Security"][0]["HistoryDetail"]
            out = sorted([[o["EndDate"][:10], float(o["Value"])] for o in pts])
            log(f"  morningstar {secid} {universe} via {host}/{tok}: {len(out)} {out[0][0]}..{out[-1][0]}"); return out if len(out) > 50 else None
        except Exception as e: log(f"  morningstar {secid} {host}/{tok}: {str(e)[:40]} :: {txt[:160].replace(chr(10),' ')!r}")
    return None
def ms_search(q):
    txt = get(f"https://www.morningstar.co.uk/uk/util/SecuritySearch.ashx?q={urllib.parse.quote(q)}&limit=10&preferedList=&source=nav&moduleId=6&ifIncludeAds=False", retries=1, timeout=20)
    if not txt: log(f"  ms search {q[:30]}: no response"); return []
    out = []
    for line in txt.splitlines():
        parts = line.split("|")
        if len(parts) >= 2:
            m = re.search(r'"i":"([^"]+)"', line); out.append((parts[0], m.group(1) if m else None, line[:200]))
    log(f"  ms search {q[:30]}: " + " || ".join(f"{a} -> {b}" for a, b, _ in out[:4]))
    return out

# ---------- FX ----------
def frankfurter(base, quote):
    txt = get(f"https://api.frankfurter.app/1999-01-04..?from={base}&to={quote}", retries=2, timeout=40)
    if not txt: return None
    try:
        rates = json.loads(txt)["rates"]; out = sorted((d, v[quote]) for d, v in rates.items() if quote in v)
        log(f"  frankfurter {base}{quote} {len(out)}"); return [[d, v] for d, v in out] if len(out) > 100 else None
    except Exception as e: log(f"  frankfurter parse {e}"); return None
def fx_series(sym):
    base, quote = sym[:3].upper(), sym[3:].upper()
    s = frankfurter(base, quote) or stooq(sym.lower())
    if not s:
        ymap = {"usdsgd": "SGD=X", "gbpsgd": "GBPSGD=X", "chfsgd": "CHFSGD=X", "eursgd": "EURSGD=X"}
        s, _ = yahoo_chart(ymap.get(sym.lower(), sym), "2000-01-01")
    return {d: v for d, v in (s or [])}

def to_sgd(series, ccy, usd, gbp, scale=1.0):
    fx = {"USD": usd, "GBP": gbp, "GBp": gbp, "SGD": None}.get(ccy, None)
    if fx is None and ccy in globals().get("FXX", {}): fx = FXX[ccy]
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
    for u in cands[:1]:
        txt = get(u, hdr, retries=1, timeout=8)
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

def ft_rows(txt):
    rows = []
    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', txt, re.S):
        cells = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)
        if len(cells) < 5: continue
        dm = re.search(r'([A-Z][a-z]+, [A-Z][a-z]+ \d{2}, \d{4})', re.sub(r'<[^>]+>', ' ', cells[0]))
        if not dm: continue
        nums = [re.sub(r'<[^>]+>', '', c).replace(',', '').strip() for c in cells[1:5]]
        try:
            d = datetime.datetime.strptime(dm.group(1), "%A, %B %d, %Y").date().isoformat()
            rows.append([d, float(nums[3])])
        except Exception: continue
    return rows

def ft_history(symbol, years=16):
    out = []; end = datetime.date.today(); dumped = False
    for k in range(years):
        a = end.replace(year=end.year-1) + datetime.timedelta(days=1)
        u = f"https://markets.ft.com/data/funds/tearsheet/historical?s={symbol}&startDate={a.strftime('%Y/%m/%d')}&endDate={end.strftime('%Y/%m/%d')}"
        txt = get(u, retries=1, timeout=20)
        if not txt: break
        rows = ft_rows(txt)
        if not rows:
            if k == 0:
                i = txt.find("Historical prices"); j = txt.find("<tbody", i if i > 0 else 0)
                log(f"  FT sample {symbol} len={len(txt)} title={re.search(r'<title>(.*?)</title>', txt, re.S).group(1).strip()[:80] if '<title>' in txt else '?'} :: " + re.sub(r"\s+", " ", txt[j:j+900] if j > 0 else txt[:300]))
            break
        out += rows; end = a - datetime.timedelta(days=1); time.sleep(0.3)
    out = sorted({d: v for d, v in out}.items())
    return [[d, v] for d, v in out] if len(out) > 50 else None

# ---------- AIA ILP funds: probe aia.com.sg from the runner, log what the fund page loads ----------
def aia_probe():
    u = "https://www.aia.com.sg/en/our-products/save-and-invest/aia-funds-information/aia-fund-price/aia-global-technology-fund"
    txt = get(u, retries=1, timeout=25)
    if not txt: log("  aia.com.sg: no response"); return
    urls = sorted(set(re.findall(r'https?://[^"\'\s<>]+(?:price|fund|chart|api|json)[^"\'\s<>]*', txt, re.I)))
    log(f"  aia.com.sg page {len(txt)} bytes; candidate urls: " + " | ".join(urls[:25]))
    for m in re.findall(r'(?:data-[a-z-]*(?:url|api|src)|fetch\(|axios\.get\()\s*=?\s*["\']([^"\']+)', txt)[:20]: log("  aia attr: " + m)

# ---------- main ----------
def main():
    old = {}
    try:
        old = json.loads(open(OUT).read()[len("window.TRACK="):-1])
    except Exception as e:
        log("no previous data.js", e)
    D = {"updated": datetime.date.today().isoformat(), "etf": old.get("etf", {}), "aia": old.get("aia", {}), "log": []}

    log("== FX"); usd = fx_series("usdsgd"); gbp = fx_series("gbpsgd"); chf = fx_series("chfsgd"); eur = fx_series("eursgd"); log(f"  USD/SGD {len(usd)} GBP/SGD {len(gbp)} CHF {len(chf)} EUR {len(eur)}")
    global FXX; FXX = {"CHF": chf, "EUR": eur}

    log("== ETFs")
    ETF = {"CSPX": "CSPX.L", "IWDA": "IWDA.L", "VWRA": "VWRA.L", "EQQQ": "EQQQ.L", "VOO": "VOO", "VT": "VT", "VWO": "VWO", "EWJ": "EWJ", "XLV": "XLV", "BND": "BND", "QQQ": "QQQ"}
    ISIN = {"CSPX": "IE00B5BMR087", "IWDA": "IE00B4L5Y983", "VWRA": "IE00BK5BQT80", "EQQQ": "IE0032077012"}
    def do_etf(item):
        k, sym = item; s = None; ccy = None
        if k in ISIN:
            s = justetf(ISIN[k], "USD"); ccy = "USD"
        else:
            s = nasdaq_hist(k); ccy = "USD"
        if not s and k == "CSPX": s, ccy = yahoo_chart(sym, "2000-01-01")
        if s:
            D["etf"].setdefault(k, {"name": k, "plain": "", "ter": None, "terSrc": ""})
            D["etf"][k]["series"] = to_sgd(s, ccy, usd, gbp); D["etf"][k]["priceOnly"] = k not in ISIN; D["etf"][k]["src"] = (f"justETF {ISIN[k]} total return (dividends included), USD" if k in ISIN else f"Nasdaq API {k} daily close (price only, dividends not included), USD") + ", converted to SGD"
            log(f"  {k} {ccy} {len(s)} {s[0][0]}..{s[-1][0]}")
        else: log(f"  {k} FAILED")
    def do_aia(item):
        secid, f = item; ser = None
        for uni in ("FOSGP$$ALL", "FOSGP$$ALL_1340", "FOALL$$ALL"):
            ser = ms_series(secid, uni, "SGD")
            if ser: f["src"] = f"Morningstar daily NAV ({secid}), SGD"; break
        if not ser: ser = fundsg_history(secid)
        if not ser:
            sym = ft_symbol(f["name"]); log(f"  FT symbol for {f['name']}: {sym}")
            if sym: ser = ft_history(sym)
        if ser:
            f["series"] = ser; f["src"] = "fundsingapore.com (Morningstar) daily NAV, SGD"
            log(f"  {f['short']} {len(ser)} {ser[0][0]}..{ser[-1][0]}")
        else: log(f"  {f['short']} FAILED")
    KNOWN_FT = {"F0HKG0704C": ["IE00B0JY6M65:USD", "IE000V48PUB9:CHF"], "F0HKG0704U": ["IE00BKDWB100:SGD"], "F0HKG070J2": ["LU0269904917:USD"], "F0HKG0704H": ["LU0058720904:USD"],
                "F0HKG0704D": ["LU0109392836:USD", "LU0260870158:EUR"], "F0HKG070IS": ["SG9999000780:SGD", "SG9999000798:SGD"]}
    KNOWN_Q = {"F0HKG0704D": "Franklin Technology Fund A Acc USD", "F0HKG070IS": "Nikko AM Shenton Japan Fund"}
    def do_under(item):
        secid, f = item; u = f.get("underlying")
        if not u: return
        ser = None; pick = None
        for nm, mid, _ in ms_search(KNOWN_Q.get(secid, u))[:3]:
            if not mid: continue
            for uni in ("FOGBR$$ALL", "FOALL$$ALL", "FOLUX$$ALL", "FOIRL$$ALL", "FOSGP$$ALL"):
                s = ms_series(mid, uni, "USD")
                if s:
                    f["underlyingSeries"] = to_sgd(s, "USD", usd, gbp); f["underlyingSrc2"] = f"Morningstar {mid} ({nm}), USD, converted to SGD"
                    log(f"  {f['short']} <- MS {mid} {nm} {len(s)}"); return
        for sym in KNOWN_FT.get(secid, []):
            s = ft_history(sym)
            if s:
                ccy = sym.split(":")[-1]; f["underlyingSeries"] = to_sgd(s, ccy, usd, gbp); f["underlyingSrc2"] = f"FT markets {sym}, converted to SGD"
                log(f"  {f['short']} <- FT {sym} {len(s)} {s[0][0]}..{s[-1][0]}"); return
        u = KNOWN_Q.get(secid, u)
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
    for it in ETF.items(): do_etf(it); time.sleep(1)
    log("== AIA probe"); aia_probe()
    log("== AIA funds (fundsingapore / FT)")
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(do_aia, list(D["aia"].items())))
    log("== Underlying funds (Yahoo search, then FT)")
    for it in list(D["aia"].items()): do_under(it)

    D["log"] = [l for l in LOG if "retry" not in l and "try " not in l][-80:]
    open(OUT, "w").write("window.TRACK=" + json.dumps(D, separators=(",", ":")) + ";")
    log(f"wrote {OUT} {len(json.dumps(D))//1024} KB")

if __name__ == "__main__":
    main()
