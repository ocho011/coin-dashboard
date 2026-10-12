#!/usr/bin/env python3
"""BTC 사이클 대시보드 빌더 — 무료 공개 API만 사용, 표준 라이브러리만 사용.

실행: python3 build.py [출력경로]  →  기본 dashboard.html (응답은 cache/ 에 6시간 캐시)
김치프리미엄은 실시간 값이라 빌드하지 않고 브라우저가 직접 조회한다.
"""
import datetime as dt
import json
import sys
import math
import statistics
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
TEMPLATE = ROOT / "template.html"
OUT = ROOT / "dashboard.html"
MAX_AGE = 6 * 3600
GENESIS = dt.date(2009, 1, 3)
HALVINGS = ["2012-11-28", "2016-07-09", "2020-05-11", "2024-04-20"]
DAY = 86400
DISPLAY_START = 1293840000  # 2011-01-01


# ---------- fetch ----------

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "btc-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def cached(name, fn):
    p = CACHE / f"{name}.json"
    if p.exists() and time.time() - p.stat().st_mtime < MAX_AGE:
        return json.loads(p.read_text())
    data = fn()
    CACHE.mkdir(exist_ok=True)
    p.write_text(json.dumps(data))
    return data


def fetch_coinmetrics():
    # community API: 기본 paging_from=end 이므로 전부 모은 뒤 정렬한다
    url = ("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?assets=btc"
           "&metrics=PriceUSD,CapMrktCurUSD,CapMVRVCur,SplyCur,HashRate"
           "&frequency=1d&page_size=10000&start_time=2010-07-18")
    rows = []
    while url:
        d = get(url)
        rows += d["data"]
        url = d.get("next_page_url")
    rows.sort(key=lambda r: r["time"])
    return rows


def fetch_bitstamp():
    start = 1313625600  # 2011-08-18, Bitstamp 일봉 시작
    out = {}
    while True:
        d = get(f"https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=86400&limit=1000&start={start}")
        rows = d["data"]["ohlc"]
        for r in rows:
            out[int(r["timestamp"])] = [float(r[k]) for k in ("open", "high", "low", "close")]
        last = int(rows[-1]["timestamp"])
        if len(rows) < 1000 or last >= time.time() - DAY:
            break
        start = last + DAY
    return [[t] + out[t] for t in sorted(out)]


def fetch_fng():
    return get("https://api.alternative.me/fng/?limit=0")["data"]


# ---------- indicators ----------

def sma(xs, n):
    out, s = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(xs, n):
    k, e, out = 2 / (n + 1), None, []
    for x in xs:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def rsi(xs, n=14):
    out = [None] * len(xs)
    if len(xs) <= n:
        return out
    g = sum(max(xs[i] - xs[i - 1], 0) for i in range(1, n + 1)) / n
    l = sum(max(xs[i - 1] - xs[i], 0) for i in range(1, n + 1)) / n
    out[n] = 100 if l == 0 else 100 - 100 / (1 + g / l)
    for i in range(n + 1, len(xs)):
        d = xs[i] - xs[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
        out[i] = 100 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def bollinger_log(xs, n=20, k=2):
    """로그 가격 공간 볼린저: 산술 σ는 지수 성장 구간에서 하단이 음수가 돼 로그축에서 깨진다."""
    lx = [math.log(x) for x in xs]
    m = sma(lx, n)
    mid, up, lo = [None] * len(xs), [None] * len(xs), [None] * len(xs)
    for i in range(n - 1, len(xs)):
        sd = statistics.pstdev(lx[i - n + 1:i + 1])
        mid[i], up[i], lo[i] = math.exp(m[i]), math.exp(m[i] + k * sd), math.exp(m[i] - k * sd)
    return mid, up, lo


def resample(daily, key):
    buckets = {}
    for t, o, h, l, c in daily:
        k = key(t)
        if k not in buckets:
            buckets[k] = [k, o, h, l, c]
        else:
            b = buckets[k]
            b[2], b[3], b[4] = max(b[2], h), min(b[3], l), c
    return [buckets[k] for k in sorted(buckets)]


def utc_date(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).date()


def ts(d):
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp())


def week_key(t):
    d = utc_date(t)
    return ts(d - dt.timedelta(days=d.weekday()))


def month_key(t):
    d = utc_date(t)
    return ts(dt.date(d.year, d.month, 1))


def rnd(x, sig=6):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return float(f"{x:.{sig}g}")


def R(xs, sig=6):
    return [rnd(x, sig) for x in xs]


def quantile(sorted_xs, q):
    i = q * (len(sorted_xs) - 1)
    lo = math.floor(i)
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (i - lo)


# ---------- panels ----------

def build_candles(daily, key, extra):
    rows = resample(daily, key)
    t = [r[0] for r in rows]
    c = [r[4] for r in rows]
    out = {"t": t, "o": R([r[1] for r in rows]), "h": R([r[2] for r in rows]),
           "l": R([r[3] for r in rows]), "c": R(c)}
    out.update(extra(c))
    return out


def monthly_extra(c):
    mid, up, lo = bollinger_log(c)
    return {"rsi": R(rsi(c), 4), "bbMid": R(mid), "bbUp": R(up), "bbLo": R(lo)}


def weekly_extra(c):
    macd = [a - b for a, b in zip(ema(c, 12), ema(c, 26))]
    sig = ema(macd, 9)
    warm = 35  # EMA 초기 구간 제거
    blank = lambda xs: [None if i < warm else x for i, x in enumerate(xs)]
    return {"ma25": R(sma(c, 25)), "ma200": R(sma(c, 200)), "ma240": R(sma(c, 240)),
            "rsi": R(rsi(c), 4), "macd": R(blank(macd), 4), "signal": R(blank(sig), 4),
            "hist": R(blank([a - b for a, b in zip(macd, sig)]), 4)}


def build_onchain(cm):
    t, price, mc, mvrv, sply, hr = [], [], [], [], [], []
    for r in cm:
        if not r.get("PriceUSD") or not r.get("CapMVRVCur"):
            continue
        t.append(int(dt.datetime.fromisoformat(r["time"][:10]).replace(tzinfo=dt.timezone.utc).timestamp()))
        price.append(float(r["PriceUSD"]))
        mc.append(float(r["CapMrktCurUSD"]))
        mvrv.append(float(r["CapMVRVCur"]))
        sply.append(float(r["SplyCur"]))
        hr.append(float(r["HashRate"]) / 1e6 if r.get("HashRate") else None)  # TH/s → EH/s
    rc = [m / v for m, v in zip(mc, mvrv)]
    # Z-Score 분모: 그 시점까지의 누적 시가총액 표준편차 (Glassnode 정의, 사후 정보 없음)
    sd, mean, m2 = [], 0.0, 0.0
    for n, m in enumerate(mc, 1):
        d = m - mean
        mean += d / n
        m2 += d * (m - mean)
        sd.append(math.sqrt(m2 / n))
    hr_f = [x if x is not None else 0.0 for x in hr]
    hr30, hr60 = sma(hr_f, 30), sma(hr_f, 60)
    p10, p20 = sma(price, 10), sma(price, 20)
    # 해시리본 Buy: 30D<60D(채굴자 항복) 이후 30D가 60D를 상향 돌파하고, 가격 10D SMA > 20D SMA 인 첫 날
    buys, capit = [], False
    for i in range(60, len(t)):
        if hr30[i] < hr60[i]:
            capit = True
        elif capit and p10[i] and p20[i] and p10[i] > p20[i]:
            buys.append(t[i])
            capit = False
    out = {"t": t, "price": R(price), "mvrv": R(mvrv, 4),
            "z": R([(m - r) / s if s else None for m, r, s in zip(mc, rc, sd)], 4),
            "nupl": R([1 - 1 / v for v in mvrv], 4),
            "rp": R([r / s for r, s in zip(rc, sply)]),
            "hr30": R(hr30, 5), "hr60": R(hr60, 5), "hrBuys": buys}
    # 2010년은 실현 시가총액·해시레이트가 0에 가까워 비율/로그축이 무너진다 → 표시는 2011년부터
    k = next(i for i, ti in enumerate(t) if ti >= DISPLAY_START)
    for key in out:
        out[key] = [x for x in out[key] if x >= DISPLAY_START] if key == "hrBuys" else out[key][k:]
    return out


def build_powerlaw(oc):
    days = [(utc_date(t) - GENESIS).days for t in oc["t"]]
    x = [math.log10(d) for d in days]
    y = [math.log10(p) for p in oc["price"]]
    n, mx, my = len(x), sum(x) / len(x), sum(y) / len(y)
    b = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / sum((xi - mx) ** 2 for xi in x)
    a = my - b * mx
    res = sorted(yi - (a + b * xi) for xi, yi in zip(x, y))
    qs = {"q05": 0.05, "q25": 0.25, "q50": 0.5, "q75": 0.75, "q95": 0.95}
    off = {k: quantile(res, q) for k, q in qs.items()}
    # 주간 샘플로 2030년 말까지 투영
    t, d = [], GENESIS + dt.timedelta(days=560)
    end = dt.date(2030, 12, 31)
    while d <= end:
        t.append(ts(d))
        d += dt.timedelta(days=7)
    lines = {k: R([10 ** (a + b * math.log10((utc_date(ti) - GENESIS).days) + o) for ti in t])
             for k, o in off.items()}
    # 가격도 같은 주간 격자로 샘플링 (차트 x축이 인덱스 기반이라 격자가 섞이면 왜곡됨)
    by_t = dict(zip(oc["t"], oc["price"]))
    price = [by_t.get(ti) for ti in t]
    last_res = y[-1] - (a + b * x[-1])
    pct = sum(1 for r in res if r <= last_res) / n
    return {"t": t, "price": price, "lines": lines, "a": a, "b": b, "pct": pct}


def build_cycles(oc, confirm_dd=0.6):
    """고점 = 신고가 갱신 전에 confirm_dd 이상 하락이 나온 ATH, 반감기 구간당 최고 1개.
    아직 확인되지 않은 마지막 ATH는 잠정 고점으로 덧붙인다."""
    t, p = oc["t"], oc["price"]
    confirmed, ath_i, hit = [], 0, False
    for i in range(1, len(t)):
        if p[i] > p[ath_i]:
            if hit:
                confirmed.append(ath_i)
            ath_i, hit = i, False
        elif p[i] <= p[ath_i] * (1 - confirm_dd):
            hit = True
    if hit:
        confirmed.append(ath_i)
    bounds = [ts(dt.date.fromisoformat(h)) for h in HALVINGS]
    epoch = lambda i: sum(t[i] >= b for b in bounds)
    best = {}
    for i in confirmed:
        e = epoch(i)
        if e not in best or p[i] > p[best[e]]:
            best[e] = i
    tops = sorted(best.values())
    if not hit:
        tops.append(ath_i)  # 잠정
    cycles = []
    for k, ti in enumerate(tops):
        start = tops[k - 1] if k else 0
        bi = min(range(start, ti + 1), key=lambda i: p[i])
        nxt_end = tops[k + 1] if k + 1 < len(tops) else len(t) - 1
        nb = min(range(ti, nxt_end + 1), key=lambda i: p[i])
        cycles.append({"bottom": t[bi], "bottomP": rnd(p[bi]), "top": t[ti], "topP": rnd(p[ti]),
                       "bullDays": (t[ti] - t[bi]) // DAY,
                       "nextBottom": t[nb], "nextBottomP": rnd(p[nb]),
                       "bearDays": (t[nb] - t[ti]) // DAY,
                       "drawdown": rnd(p[nb] / p[ti] - 1, 4),
                       "open": k + 1 == len(tops) and not hit, "dataStart": k == 0})
    return cycles


def main():
    cm = cached("coinmetrics", fetch_coinmetrics)
    daily = cached("bitstamp", fetch_bitstamp)
    fng = cached("fng", fetch_fng)
    oc = build_onchain(cm)
    data = {
        "asof": {"onchain": oc["t"][-1], "candles": daily[-1][0], "built": int(time.time())},
        "monthly": build_candles(daily, month_key, monthly_extra),
        "weekly": build_candles(daily, week_key, weekly_extra),
        "onchain": oc,
        "powerlaw": build_powerlaw(oc),
        "cycles": build_cycles(oc),
        "halvings": [ts(dt.date.fromisoformat(h)) for h in HALVINGS],
        "fng": {"t": [int(r["timestamp"]) for r in reversed(fng)],
                "v": [int(r["value"]) for r in reversed(fng)]},
    }
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(TEMPLATE.read_text().replace("__DATA__", blob))
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
