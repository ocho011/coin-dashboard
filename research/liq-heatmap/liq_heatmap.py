"""추정 청산 히트맵 프로토타입 (Binance USDT-M, 최근 30일, 5분봉).

모델: 봉마다 OI 증가분을 신규 포지션으로 보고, 테이커 매수 비중으로 롱/숏을 나눈 뒤
가정한 레버리지 분포로 청산가를 계산해 가격 구간별로 누적한다.
- OI 감소 → 전체 잔량을 같은 비율로 축소 (비례 청산 가정)
- 가격이 청산가를 통과 → 해당 구간 제거 (청산됨)

표준 라이브러리만 사용. 출력은 plotly.js(CDN) HTML 한 장.
사용: python3 liq_heatmap.py [SYMBOL] [--refresh]
"""

from __future__ import annotations

import json
import math
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://fapi.binance.com"
BAR_MS = 300_000
DAYS = 29  # openInterestHist는 최근 30일만 제공 → 경계 여유 1일

# 가정 — 검증 불가, 결과를 가장 크게 좌우하는 파라미터.
# 2026-10-16 CoinGlass 시각 대조 후 저배율 축소(5x 제거, 10x 25%→10%).
LEVERAGE = {10: 0.10, 25: 0.25, 50: 0.30, 100: 0.35}
MMR = 0.004  # 유지증거금률 근사

BIN_PCT = 0.001  # 가격 구간 폭 = 0.1% (로그 구간, 심볼 무관)
SNAP_EVERY = 12  # 12 × 5분 = 1시간마다 스냅샷
DISPLAY_CUT = 0.15  # 색상 상한의 15% 미만은 표시하지 않음 (CoinGlass 'Liquidity Threshold' 유사)

HERE = Path(__file__).parent


class LiqBook:
    """가격 구간별 청산 대기 물량 (USD 명목가치).

    OI 감소 시 전량 비례 축소는 전역 배율 ``scale``로 처리해 봉당 O(1)로 만든다.
    저장값 × scale = 실제값.
    """

    def __init__(self, ref_px: float) -> None:
        self.lo = ref_px * 0.2
        self.step = math.log1p(BIN_PCT)
        self.n = int(math.log(25) / self.step) + 1  # ref × 0.2 ~ ref × 5
        self.longs = [0.0] * self.n  # 롱 청산 — 현재가 아래
        self.shorts = [0.0] * self.n  # 숏 청산 — 현재가 위
        self.scale = 1.0
        self.long_top = -1  # 0이 아닌 롱 구간의 상한 (이상은 모두 0)
        self.short_bot = self.n  # 0이 아닌 숏 구간의 하한

    def idx(self, px: float) -> int:
        return min(max(int(math.log(px / self.lo) / self.step), 0), self.n - 1)

    def price(self, j: int) -> float:
        return self.lo * math.exp((j + 0.5) * self.step)

    def feed(self, h: float, l: float, c: float, vol: float, buy: float,
             oi0: float | None, oi1: float | None) -> None:
        # 1) 이번 봉이 통과한 구간 청산
        il = self.idx(l)
        for j in range(il, self.long_top + 1):
            self.longs[j] = 0.0
        self.long_top = min(self.long_top, il - 1)
        ih = self.idx(h)
        for j in range(self.short_bot, ih + 1):
            self.shorts[j] = 0.0
        self.short_bot = max(self.short_bot, ih + 1)

        # 2) OI 변화 반영 (봉 시작 → 봉 끝)
        if oi0 is None or oi1 is None:
            return
        if oi1 > oi0:
            px = (h + l + c) / 3
            buy_frac = buy / vol if vol else 0.5
            notional = (oi1 - oi0) * px / self.scale
            for lev, w in LEVERAGE.items():
                jl = self.idx(px * (1 - 1 / lev + MMR))
                js = self.idx(px * (1 + 1 / lev - MMR))
                self.longs[jl] += notional * buy_frac * w
                self.shorts[js] += notional * (1 - buy_frac) * w
                self.long_top = max(self.long_top, jl)
                self.short_bot = min(self.short_bot, js)
        elif oi1 < oi0 and oi0 > 0:
            self.scale *= oi1 / oi0
            if self.scale < 1e-100:
                self.longs = [x * self.scale for x in self.longs]
                self.shorts = [x * self.scale for x in self.shorts]
                self.scale = 1.0

    def level(self, j: int) -> float:
        return (self.longs[j] + self.shorts[j]) * self.scale

    def strength(self, px: float, side: str, width: float) -> float:
        """px ±width 안의 한쪽 청산 물량 (side='long' | 'short')."""
        arr = self.longs if side == "long" else self.shorts
        return sum(arr[self.idx(px * (1 - width)):self.idx(px * (1 + width)) + 1]) * self.scale

    def totals(self) -> tuple[float, float]:
        return sum(self.longs) * self.scale, sum(self.shorts) * self.scale


def get(path: str, **params: object) -> list:
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)


def fetch(symbol: str, start: int, end: int) -> dict:
    oi: dict[int, float] = {}
    t = start
    while t < end:
        rows = get("/futures/data/openInterestHist", symbol=symbol, period="5m",
                   limit=500, startTime=t, endTime=min(t + 500 * BAR_MS, end))
        for r in rows:
            oi[int(r["timestamp"])] = float(r["sumOpenInterest"])
        t += 500 * BAR_MS
        time.sleep(0.2)

    klines: list[list] = []
    t = start
    while t < end:
        rows = get("/fapi/v1/klines", symbol=symbol, interval="5m",
                   limit=1500, startTime=t, endTime=end)
        if not rows:
            break
        klines += rows
        t = rows[-1][0] + BAR_MS
        time.sleep(0.2)
    return {"oi": oi, "klines": klines}


def load(symbol: str, refresh: bool) -> dict:
    cache = HERE / f"cache_{symbol}.json"
    if cache.exists() and not refresh:
        d = json.loads(cache.read_text())
        d["oi"] = {int(k): v for k, v in d["oi"].items()}
        return d
    end = int(time.time() * 1000) // BAR_MS * BAR_MS
    d = fetch(symbol, end - DAYS * 86_400_000, end)
    cache.write_text(json.dumps(d))
    return d


def simulate(d: dict) -> dict:
    oi, kl = d["oi"], d["klines"]
    book = LiqBook(float(kl[0][4]))
    snaps, candles = [], []
    skipped = 0
    h_o = h_h = h_l = None

    for i, k in enumerate(kl):
        t, o, h, l, c = k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4])
        o0, o1 = oi.get(t), oi.get(t + BAR_MS)
        skipped += o0 is None or o1 is None
        book.feed(h, l, c, float(k[5]), float(k[9]), o0, o1)

        if h_o is None:
            h_o, h_h, h_l = o, h, l
        h_h, h_l = max(h_h, h), min(h_l, l)
        if (i + 1) % SNAP_EVERY == 0:
            ts = datetime.fromtimestamp((t + BAR_MS) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")
            snaps.append((ts, [book.level(j) for j in range(book.n)]))
            candles.append((ts, h_o, h_h, h_l, c))
            h_o = None

    last_px = float(kl[-1][4])
    remain_l, remain_s = book.totals()
    last_oi = oi[max(oi)] * last_px
    top = sorted(range(book.n), key=book.level, reverse=True)[:6]
    print(f"bars={len(kl)} snapshots={len(snaps)} skipped(OI 결측)={skipped}")
    print(f"잔여 롱 ${remain_l/1e9:.2f}B · 숏 ${remain_s/1e9:.2f}B · 현재 OI ${last_oi/1e9:.2f}B")
    print(f"현재가 {last_px:,.4g} · 상위 클러스터:")
    for j in sorted(top):
        side = "숏(위)" if book.shorts[j] > book.longs[j] else "롱(아래)"
        print(f"  {book.price(j):>10,.5g}  ${book.level(j)/1e6:7.1f}M  {side}")
    assert remain_l + remain_s <= last_oi * 1.05, "잔여 명목가치가 현재 OI를 초과 — 모델 버그"
    return {"book": book, "snaps": snaps, "candles": candles}


def render(symbol: str, r: dict) -> Path:
    book = r["book"]
    y0 = book.idx(min(c[3] for c in r["candles"]) * 0.92)
    y1 = book.idx(max(c[2] for c in r["candles"]) * 1.08)
    ys = [float(f"{book.price(j):.5g}") for j in range(y0, y1)]
    xs = [s[0] for s in r["snaps"]]
    z = [[r["snaps"][x][1][j] / 1e6 for x in range(len(xs))] for j in range(y0, y1)]
    flat = sorted(v for row in z for v in row if v > 0)
    zmax = flat[int(len(flat) * 0.99)] if flat else 1
    cut = zmax * DISPLAY_CUT
    z = [[round(v, 2) if v >= cut else None for v in row] for row in z]
    data = {"x": xs, "y": ys, "z": z, "zmax": zmax,
            "c": [list(col) for col in zip(*r["candles"])]}
    lev = " · ".join(f"{k}x {int(v*100)}%" for k, v in LEVERAGE.items())
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{symbol} 추정 청산 히트맵</title>
<script src="https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js"></script>
<style>body{{margin:0;background:#0b0e14;color:#cbd5e1;font:13px system-ui}}
#p{{width:100vw;height:92vh}}.n{{padding:6px 12px}}</style></head><body>
<div class="n">{symbol} 추정 청산 히트맵 (Binance OI 5분, 최근 {DAYS}일) · 가정 레버리지 {lev} · MMR {MMR*100}% ·
구간 {BIN_PCT*100}% · 색상 상한 99%ile(${zmax:,.1f}M), {int(DISPLAY_CUT*100)}% 미만 숨김 · 초반은 워밍업</div>
<div id="p"></div><script>
const d={json.dumps(data)};
Plotly.newPlot('p',[
 {{type:'heatmap',x:d.x,y:d.y,z:d.z,zmin:0,zmax:d.zmax,colorscale:'Viridis',
   colorbar:{{title:'$M'}},hovertemplate:'%{{x}}<br>%{{y}}<br>$%{{z}}M<extra></extra>'}},
 {{type:'candlestick',x:d.c[0],open:d.c[1],high:d.c[2],low:d.c[3],close:d.c[4],
   increasing:{{line:{{color:'#f8fafc'}}}},decreasing:{{line:{{color:'#f43f5e'}}}},showlegend:false}}
],{{paper_bgcolor:'#0b0e14',plot_bgcolor:'#440154',font:{{color:'#cbd5e1'}},
  margin:{{l:60,r:20,t:10,b:40}},xaxis:{{rangeslider:{{visible:false}}}},yaxis:{{side:'right'}}}},
 {{responsive:true}});
</script></body></html>"""
    out = HERE / f"{symbol.lower()}_liq_heatmap.html"
    out.write_text(html)
    print(f"→ {out}")
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sym = args[0].upper() if args else "BTCUSDT"
    render(sym, simulate(load(sym, "--refresh" in sys.argv)))
