#!/usr/bin/env python3
"""btc15m 수집기: OpenMarket Data API → Supabase flow_15m.

매 실행마다 최근 LOOKBACK 시간의 15분봉을 받아 (t, series) 기준으로 upsert한다.
맥북이 잠들어 실행을 건너뛰어도 다음 실행이 공백을 메운다(LOOKBACK 범위 안에서).
첫 실행은 --backfill 로 OpenMarket Free 한도(7일)까지 받는다.

키는 ~/.config/btc15m/env 에서 읽는다(OPENMARKET_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY).
표준 라이브러리만 쓴다.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ENV = os.path.expanduser("~/.config/btc15m/env")
OM = "https://api.openmarket.xyz/v1/points"
LOOKBACK = 6 * 3600
BACKFILL = 7 * 86400 - 3600      # Free 한도 7일, 경계 여유 1시간
KEEP_DAYS = 8
GAP = 6.5                        # Free: 분당 10 weight → 호출 간격

# (series 접두어, 파라미터). TRADE_AGG는 _buy/_sell, LIQUIDATION_AGG는 _long/_short가 붙는다.
TRADES = [
    ("cb", {"exchange": "COINBASE", "rawSymbol": "BTC-USD"}),
    ("hlf", {"exchange": "HYPERLIQUID_FUTURES", "rawSymbol": "BTC"}),
    ("hls", {"exchange": "HYPERLIQUID", "rawSymbol": "@142"}),   # UBTC/USDC
]
VENUES = [  # OI·청산 공통
    ("bn", {"exchange": "BINANCE_FUTURES", "rawSymbol": "BTCUSDT"}),
    ("by", {"exchange": "BYBIT", "rawSymbol": "BTCUSDT"}),
    ("ok", {"exchange": "OKEX_SWAP", "rawSymbol": "BTC-USDT-SWAP"}),
    ("hl", {"exchange": "HYPERLIQUID_FUTURES", "rawSymbol": "BTC"}),
]


def load_env():
    env = {}
    with open(ENV) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def http(url, headers, data=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    return json.loads(body) if body else None


def om_points(key, params, start, period):
    q = {"interval": "FIFTEEN_MINUTES", "from": start, "period": period, **params}
    url = OM + "?" + urllib.parse.urlencode(q)
    for attempt in range(3):
        try:
            return http(url, {"X-OpenMarket-Key": key})["series"]
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(30)
                continue
            raise


def iso(s):
    return datetime.fromtimestamp(s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect(key, start, period):
    rows = {}  # (t, series) -> value

    def put(t, series, v):
        rows[(iso(t), series)] = float(v)

    jobs = []
    for name, p in TRADES:
        for side in ("BUY", "SELL"):
            jobs.append((f"{name}_{side.lower()}", {"type": "TRADE_AGG", "side": side,
                         "transform.normalize.quote": "USD", **p}, "volume"))
    for name, p in VENUES:
        jobs.append((f"oi_{name}", {"type": "OPEN_INTEREST_AGG", "transform.normalize.quote": "USD", **p}, "close"))
        jobs.append((f"liq_{name}", {"type": "LIQUIDATION_AGG", **p}, "liquidations"))

    failed = []
    for i, (series, params, field) in enumerate(jobs):
        if i:
            time.sleep(GAP)
        try:
            out = om_points(key, params, start, period)
        except Exception as e:  # 한 시리즈 실패가 나머지를 막지 않게 기록만 하고 계속
            failed.append(f"{series}: {e}")
            continue
        for s in out:
            sid = s["id"]
            name = series
            if params["type"] == "LIQUIDATION_AGG":
                # 롱 청산 = SELL 시리즈, 숏 청산 = BUY 시리즈 (BTC 수량)
                name = f"{series}_{'long' if sid.get('side') == 'SELL' else 'short'}"
            for pt in s.get("points", []):
                P = pt["Point"]
                put(P["timestamp"]["s"], name, P[field])
    return rows, failed, len(jobs)


def upsert(env, rows):
    url = env["SUPABASE_URL"] + "/rest/v1/flow_15m?on_conflict=t,series"
    h = {"apikey": env["SUPABASE_SERVICE_KEY"], "Authorization": "Bearer " + env["SUPABASE_SERVICE_KEY"],
         "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"}
    items = [{"t": t, "series": s, "value": v} for (t, s), v in rows.items()]
    for i in range(0, len(items), 1000):
        http(url, h, json.dumps(items[i:i + 1000]).encode(), "POST")
    cutoff = iso(time.time() - KEEP_DAYS * 86400)
    http(env["SUPABASE_URL"] + "/rest/v1/flow_15m?t=lt." + cutoff, h, method="DELETE")
    return len(items)


def main():
    env = load_env()
    span = BACKFILL if "--backfill" in sys.argv else LOOKBACK
    now = int(time.time())
    start = (now - span) // 900 * 900
    rows, failed, n = collect(env["OPENMARKET_KEY"], start, now - start + 900)
    wrote = upsert(env, rows) if rows else 0
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"{stamp} span={span // 3600}h calls={n} rows={wrote} failed={len(failed)}")
    for f in failed:
        print("  FAIL", f)
    sys.exit(1 if len(failed) == n else 0)


if __name__ == "__main__":
    main()
