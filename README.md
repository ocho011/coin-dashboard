# btc-cycle-dashboard

무료 공개 데이터로 만든 코인 대시보드. GitHub Actions가 매일 다시 빌드해 GitHub Pages에 배포한다.

| 페이지 | 경로 | 소스 |
|---|---|---|
| BTC 사이클 지표 | `/` | `build.py` + `template.html` (빌드 시 데이터 내장) |
| 추정 청산 히트맵 | `/heatmap/` | `heatmap.html` (정적, 브라우저가 Binance API를 직접 조회해 계산) |

청산 히트맵은 미결제약정 증가분에 가정한 레버리지 분포를 적용한 추정치다. 모델·한계·검증 결과는 페이지 하단에 있다.

## 패널

| 구분 | 지표 |
|---|---|
| 밸류에이션 | 파워로 밸류에이션 밴드(2030 외삽), MVRV · MVRV Z-Score, NUPL, 실현가격 |
| 추세 | 월봉 + RSI(14), 월봉 볼린저밴드(로그), 주봉 25주 MA + MACD, 200·240주 MA + 주간 RSI |
| 네트워크 | 해시리본(30D/60D) + Buy 신호 |
| 심리 | 공포·탐욕 지수, 김치프리미엄(브라우저 실시간) |
| 이력 | 사이클 고점·저점 표 |

지표 정의와 계산식은 페이지 하단에 적혀 있다.

## 로컬 실행

```sh
python3 build.py            # → dashboard.html (API 응답은 cache/ 에 6시간 캐시)
python3 build.py out/x.html # 출력 경로 지정
```

파이썬 표준 라이브러리만 쓴다. 차트는 [lightweight-charts](https://github.com/tradingview/lightweight-charts) 4.2.0(unpkg CDN). 청산 히트맵은 [plotly.js](https://github.com/plotly/plotly.js) 2.35.2(jsDelivr CDN), 로컬에서는 `heatmap.html`을 브라우저로 바로 열면 된다.

## 배포

`.github/workflows/pages.yml` — 매일 01:30 UTC, main push, 수동 실행 시 빌드 후 Pages 배포.

## 데이터 출처와 이용 조건

| 데이터 | 출처 | 조건 |
|---|---|---|
| 가격·시가총액·MVRV·공급량·해시레이트 | [Coin Metrics Community API](https://coinmetrics.io/) | CC BY-NC 4.0 — 출처 표기, 비상업적 이용 |
| 월봉·주봉 OHLC | Bitstamp 공개 API | — |
| 공포·탐욕 지수 | [alternative.me](https://alternative.me/crypto/fear-and-greed-index/) | 데이터 옆 출처 표기 |
| 청산 히트맵 | Binance USDT-M 선물 공개 API (openInterestHist, klines) | 빌드하지 않고 브라우저가 직접 조회. 최근 30일만 제공 |
| 김치프리미엄 | Upbit · Binance 공개 시세, [Rates By Exchange Rate API](https://www.exchangerate-api.com) | 환율 데이터 재배포 금지 → 빌드하지 않고 브라우저가 직접 조회 |

Coin Metrics 데이터가 비상업 라이선스이므로 이 프로젝트도 비상업적 개인 연구용이다. 투자 조언이 아니다.

## 라이선스

코드는 [MIT](LICENSE). 단, 대시보드가 표시하는 데이터는 위 표의 각 제공자 조건을 따른다(MIT 적용 대상 아님).
