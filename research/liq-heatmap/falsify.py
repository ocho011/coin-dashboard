"""PREREG.md의 반증 테스트 실행기. 규칙은 PREREG.md가 권위 — 여기서 바꾸지 말 것.

사용: python3 falsify.py            (BTCUSDT ETHUSDT ZECUSDT)
데이터는 data/<SYMBOL>/ 에 받아 둔다 (data.binance.vision, 재실행 시 재사용).
"""

from __future__ import annotations

import csv
import io
import math
import statistics
import sys
import urllib.request
import zipfile
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from liq_heatmap import LiqBook

ARCHIVE = "https://data.binance.vision/data/futures/um"
START, END = date(2025, 1, 1), date(2026, 8, 31)
WARMUP_MS = 7 * 86_400_000
M5, M15, H1 = 300_000, 900_000, 3_600_000
K = 3  # aurora SWEEP_RETURN_BARS
FWD = 16  # 15m × 16 = 4h
WIDTH = 0.0015  # 풀 ±0.15%
DATA = Path(__file__).parent / "data"


def _get(url: str, dest: Path) -> Path | None:
    if dest.exists():
        return dest
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            dest.write_bytes(r.read())
        return dest
    except Exception:  # 아카이브 결측일 — 호출부가 개수를 보고한다
        return None


def _rows(path: Path) -> list[list[str]]:
    with zipfile.ZipFile(path) as z:
        text = z.read(z.namelist()[0]).decode()
    rows = list(csv.reader(io.StringIO(text)))
    return rows[1:] if rows and not rows[0][0][:1].isdigit() else rows


def load(sym: str) -> tuple[list[tuple], dict[int, float]]:
    d = DATA / sym
    d.mkdir(parents=True, exist_ok=True)
    months, m = [], START.replace(day=1)
    while m <= END:
        months.append(m)
        m = (m + timedelta(days=32)).replace(day=1)
    days = [START + timedelta(days=i) for i in range((END - START).days + 1)]
    jobs = [(f"{ARCHIVE}/monthly/klines/{sym}/5m/{sym}-5m-{m:%Y-%m}.zip", d / f"k-{m:%Y-%m}.zip")
            for m in months]
    jobs += [(f"{ARCHIVE}/daily/metrics/{sym}/{sym}-metrics-{x}.zip", d / f"m-{x}.zip") for x in days]
    with ThreadPoolExecutor(8) as ex:
        got = list(ex.map(lambda j: _get(*j), jobs))
    kfiles, mfiles = got[:len(months)], got[len(months):]
    print(f"[{sym}] klines 월 {sum(f is not None for f in kfiles)}/{len(months)}, "
          f"metrics 일 {sum(f is not None for f in mfiles)}/{len(days)}")

    bars = []
    for f in kfiles:
        if f:
            for r in _rows(f):
                bars.append((int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                             float(r[5]), float(r[9])))
    bars.sort()
    oi: dict[int, float] = {}
    for f in mfiles:
        if f:
            for r in _rows(f):
                ts = datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                oi[int(ts.timestamp() * 1000)] = float(r[2])
    return bars, oi


def agg(bars: list[tuple], ms: int) -> list[tuple]:
    """5m → ms 봉 (open_ms, o, h, l, c). 구간이 다 차지 않은 봉은 버린다."""
    out, cur = [], []
    for b in bars:
        if cur and b[0] // ms != cur[0][0] // ms:
            if len(cur) == ms // M5:
                out.append((cur[0][0] // ms * ms, cur[0][1], max(x[2] for x in cur),
                            min(x[3] for x in cur), cur[-1][4]))
            cur = []
        cur.append(b)
    return out


def events(sym: str) -> list[dict]:
    bars, oi = load(sym)
    book = LiqBook(bars[0][4])
    by15: dict[int, list[tuple]] = {}
    for b in bars:
        by15.setdefault(b[0] // M15 * M15, []).append(b)
    q = agg(bars, M15)
    h1 = agg(bars, H1)

    # 1h 피벗: 인덱스 i는 i+1 봉 마감 시점에 유효
    pivots = []  # (active_ms, price, is_high)
    for i in range(1, len(h1) - 1):
        p, c, n = h1[i - 1], h1[i], h1[i + 1]
        if c[2] > p[2] and c[2] > n[2]:
            pivots.append((n[0] + H1, c[2], True))
        if c[3] < p[3] and c[3] < n[3]:
            pivots.append((n[0] + H1, c[3], False))
    pivots.sort()

    highs: list[float] = []
    lows: list[float] = []
    pi, out, t0 = 0, [], bars[0][0] + WARMUP_MS
    for m, (t, o, h, l, c) in enumerate(q):
        while pi < len(pivots) and pivots[pi][0] <= t:
            _, px, is_high = pivots[pi]
            (highs if is_high else lows).insert(bisect_left(highs if is_high else lows, px), px)
            pi += 1

        hit_h = highs[:bisect_left(highs, h)]
        del highs[:len(hit_h)]
        cut = bisect_right(lows, l)
        hit_l = lows[cut:]
        del lows[cut:]

        if t >= t0 and m + FWD < len(q):
            px_oi = (oi.get(t) or 0) * c
            fwd = q[m:m + K]
            for lvl, is_high in [(x, True) for x in hit_h] + [(x, False) for x in hit_l]:
                s = book.strength(lvl, "short" if is_high else "long", WIDTH)
                sweep = any((b[4] < lvl) if is_high else (b[4] > lvl) for b in fwd)
                ret = (c - q[m + FWD][4]) / c if is_high else (q[m + FWD][4] - c) / c
                if px_oi > 0:
                    out.append({"s": s / px_oi, "sweep": sweep, "ret": ret * 100})

        for b in by15.get(t, []):  # 관통 판정 후 이번 15m 봉의 5m 봉을 장부에 반영
            book.feed(b[2], b[3], b[4], b[5], b[6], oi.get(b[0]), oi.get(b[0] + M5))
    return out


def ztest(a: list[bool], b: list[bool]) -> tuple[float, float, float]:
    pa, pb = sum(a) / len(a), sum(b) / len(b)
    p = (sum(a) + sum(b)) / (len(a) + len(b))
    se = math.sqrt(p * (1 - p) * (1 / len(a) + 1 / len(b)))
    z = (pa - pb) / se
    return pa, pb, math.erfc(abs(z) / math.sqrt(2))


def welch(a: list[float], b: list[float]) -> tuple[float, float, float]:
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    se = math.sqrt(statistics.variance(a) / len(a) + statistics.variance(b) / len(b))
    return ma, mb, math.erfc(abs((ma - mb) / se) / math.sqrt(2))  # 대표본 정규 근사


def main() -> None:
    syms = sys.argv[1:] or ["BTCUSDT", "ETHUSDT", "ZECUSDT"]
    signs = {}
    for sym in syms:
        ev = events(sym)
        ev.sort(key=lambda e: e["s"])
        n3 = len(ev) // 3
        lo, hi = ev[:n3], ev[-n3:]
        overlap = sum(e["s"] > 0 for e in ev) / len(ev)
        pa, pb, p1 = ztest([e["sweep"] for e in hi], [e["sweep"] for e in lo])
        ma, mb, p2 = welch([e["ret"] for e in hi], [e["ret"] for e in lo])
        signs[sym] = pa - pb
        print(f"[{sym}] 이벤트 {len(ev)} · 강도>0 비율 {overlap:.1%} · "
              f"하위1/3 강도 상한 {lo[-1]['s']:.2e} · 상위1/3 하한 {hi[0]['s']:.2e}")
        print(f"  1차 스윕률  상위 {pa:.1%} vs 하위 {pb:.1%}  (차 {pa-pb:+.1%}, p={p1:.3g})")
        print(f"  2차 4h 반전 상위 {ma:+.3f}% vs 하위 {mb:+.3f}%  (차 {ma-mb:+.3f}%p, p={p2:.3g})")
        if sym == "BTCUSDT":
            btc_p = p1

    if "BTCUSDT" in signs and len(signs) == 3:
        same = all(math.copysign(1, v) == math.copysign(1, signs["BTCUSDT"]) for v in signs.values())
        verdict = btc_p < 0.01 and same
        print(f"판정: {'정보 있음' if verdict else 'H0 유지 (배선 안 함)'} "
              f"— BTC p={btc_p:.3g} (<0.01 필요), 부호 일치 {same}")


if __name__ == "__main__":
    main()
