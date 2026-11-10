# D103 0단계: Bybit TradFi 무기한 실현 가능성 점검 (1차, 공개 문서·메타데이터)

- 작성: Claude Code, 2026-11-10. 계약 원문은 04 3.6이다.
- 이번 1차 범위는 **공식 문서와 공개 메타데이터(`instruments-info`)만**이다. 시세(ticker·kline·orderbook)는 받지 않았다. 수익률 계산, 키가 필요한 API 호출, 주문 호출도 하지 않았다.
- 시세가 필요한 2.3(유동성·스프레드)은 수집 계획을 게시한 뒤 2차에서 한다.

## 관측 범위 기록 (04 3.6 3절)

| 항목 | 내용 |
|---|---|
| 요청 | `GET https://api.bybit.com/v5/market/instruments-info?category=linear&symbolType={stock,ETF,commodity,forex}&limit=1000`, 4회(각 1페이지, `nextPageCursor` 빈 값) |
| 시각 | 2026-11-10T13:14Z |
| 응답 sha256 | stock `d90396f8590e33ea0c24e5114197b31a9e5cf007966da5d441deb73ed5b1da42`, ETF `63348efced9d1cb89c90382520b347c689ae06cd2b4ea4c2717f10224f2c7fc3`, commodity `dddc9076438c625df764433ba579f446c9ff34abb8d8d13df531cb837464ad95`, forex `c6f2f8805a8a49a48854c777371fc4199ed89aefd242ba40aceb8156a19428d0` (원문은 저장소에 넣지 않음) |
| 받은 것 | 계약 메타데이터(심볼, 상장 시각, 레버리지, 펀딩 주기·상한, 가격 제한 비율, `marketRegion`, `underlyingTicker`). **가격·거래량 없음** |
| 암호자산 관측 | 없음. 응답 305개 중 D98 풀 암호자산 심볼·baseCoin과 겹치는 것 0개 |
| 용도 | 설계 참고용. 이 메타데이터로 비용 보정이나 판정 구간을 정하지 않았다 |

## 1. 상품 개요 (메타데이터, 2026-11-10)

| symbolType | 개수 | 시장(`marketRegion`) | 상장 | 최대 레버리지 | 펀딩 |
|---|---:|---|---|---|---|
| stock | 238 | US 210, HK 19, KR 7, CN 2 | 2026-04-21 ~ 2026-11-06 | 25배 205개, 50배 16, 100배 13, 20배 4 | 전부 8h, 상한 대부분 ±2% |
| ETF | 59 | US 56, HK 2, KR 1 | 2026-05-07 ~ 2026-10-30 | 25배 49개 외 | 전부 8h |
| commodity | 4 | (없음) | 2026-03-09 ~ 2026-05-13 | 100~150배 | 8h 2개, 4h 2개 |
| forex | 4 | (없음) | 2026-09-08 ~ 2026-11-03 | 100배 | 8h |

- 모두 `LinearPerpetual`, USDT 정산, `status = Trading`이다. D98 스냅샷(2026-11-02) 대비 7개가 새로 생겼고, 사라진 것은 없다.
- 한국 종목: 삼성전자, SK하이닉스(최대 100배), 현대차, LG전자, 네이버, 한미반도체, 삼성전기, KODEX 200 ETF.
- 원자재: 금(XAU), 은(XAG), WTI, 브렌트. 외환: EURUSD, GBPUSD, USDJPY, USDBRL.
- 가격 제한 비율 `priceLimitRatioX`: 주식·ETF는 전부 0.03이다. 원자재·외환은 0.01~0.15다.
- **과거는 가장 긴 주식도 약 6.5개월이다.**

## 2. 상품 규칙 (04 3.6 2.1)

| 항목 | 확인 결과 | 출처 |
|---|---|---|
| 거래 시간 | 24/7 거래. 세션 구분과 시작·종료 시각은 Bybit 재량이며 바뀔 수 있다 | 도움말 TradFi 소개 |
| 지수 가격 | 정규장: 1초마다 구성 요소 가중 평균. 프리·애프터: 빠른 EWMA(반감기 120초). 야간: 느린 EWMA(600초). **휴장: 갱신이 멈춘 구성 요소는 빠지고 느린 EWMA**. 세션이 바뀌면 직전 지수에서 시작해 재개 후 약 5초 안에 표준 가격으로 돌아온다 | 도움말 TradFi 소개, API TradFi Integration |
| mark 가격 | `Median(Price1, Price2, 최종 체결가)`를 지수 ±상한으로 묶는다. 상한은 단일 주식 5%, ETF·원자재 3%. 일부 페이지는 3%로 적어 계약별로 확인 필요 | 도움말 TradFi 소개, Mark Price 도움말 |
| 펀딩 | 일반 USDT 무기한과 같은 방식. 주기·상한은 위 메타데이터 | 도움말, 메타데이터 |
| 수수료 | 일반 USDT 무기한과 같은 구조에 TradFi 할인 요율 적용(Pre-IPO 제외). **구체 요율은 미확인** | 도움말, 상장 공지 |
| 주말 제한 | "주말에는 일부 심볼에 포지션 위험 통제가 걸려 **reduce-only 주문만 허용**될 수 있다" | API instruments-info 문서 |
| 기업 이벤트 | 배당: 보유자에게 지급하지 않고 가격·펀딩에 반영. 분할·병합: 조정 기간 동안 **거래가 중단**되고 상태는 `Trading`으로 남는다. 그동안 가격 데이터가 없다. 과거 kline은 분할 비율로 조정된다. 옵션은 심볼 이름이 바뀔 수 있어 `symbolId`로 추적 | API TradFi Integration, instruments-info |
| 상장 폐지 | **미확인** | — |
| 가격 제한·거래 중단 | `priceLimitRatioX/Y`(2026-05 산식 변경), 분할 시 중단. 실적 발표 같은 이벤트 중단 규칙은 **미확인** | instruments-info |

## 3. API·보호 호환성 (04 3.6 2.2)

| 항목 | 상태 | 내용 |
|---|---|---|
| 주문·포지션 API | 확인 | 일반 V5 주문 엔드포인트와 같은 linear 필드(`leverageFilter`·`priceFilter`·`lotSizeFilter`·`fundingInterval`)를 쓴다 |
| 거래 동의 | 확인, **운영 영향** | 주식·금속 무기한은 거래 동의가 필요하다. 웹에서는 **마스터 계정만** 동의할 수 있고, 마스터가 동의하면 서브 계정도 거래할 수 있다. API 동의(`POST /v5/user/agreement`)는 이체 또는 **출금 권한** 키가 필요하다. D75·D92(출금 권한 없는 키)와 충돌하므로 **사용자가 웹 UI에서 한 번 동의**하는 방식이 맞다 |
| 단방향·격리 마진 | 미확인 | 문서에 TradFi 예외는 없다. 데모에서 확인할 항목이다 |
| 서브 계정 | 부분 확인 | 마스터 동의 뒤 거래 가능(위). 데모 계정은 서브 계정에도 별도로 있다 |
| 데모 | **미확인** | 데모 API는 "모든 API를 지원하지는 않는다"고 한다. TradFi 무기한이 데모 상품 목록에 있는지 확인되지 않았다 |
| 손절 부착 | 문서상 같음, TradFi 전용 설명 없음 | Full은 Market만, Partial은 Limit 가능. 포지션이 닫히면 취소되고 수량은 포지션에 맞춰 조정된다 |
| `reduceOnly`·`closeOnTrigger` | 문서상 같음 | `closeOnTrigger`는 증거금이 부족해도 손절이 닫히도록 다른 주문을 줄일 수 있다 |
| `triggerBy` | 확인 | LastPrice·IndexPrice·MarkPrice. **휴장 중에는 지수가 느린 EWMA로 거의 멈추므로** Index·Mark 기준 손절은 장 재개 때 한꺼번에 트리거될 수 있다(추론, 미검증) |
| 장 재개 갭 | 위험 확인 | 시장가는 IOC 지정가로 바뀌고, mark 대비 허용 범위 안에 호가가 없으면 **체결되지 않거나 취소**된다. 조건부 주문은 슬리피지 허용 설정을 쓸 수 없다. 갭으로 손절가를 건너뛰었을 때의 실제 동작은 데모 검증이 필요하다 |
| 강제청산 | 미확인 | 증거금·청산 방식은 일반 무기한과 같다고 하지만, 휴장 중 mark 상한(±3~5%)과 강제청산의 관계는 문서에 없다 |
| 한국 거주자 | **미확인** | TradFi는 "일부 지역 제한"이라고만 한다. 언론 기준 EEA·미국·중국 본토 등은 불가. 한국 포함 여부는 사용자가 앱에서 거래 동의 화면이 열리는지로 확인하는 것이 가장 확실하다 |

**"서버 손절이 있다"와 "휴장 중에도 보호된다"는 별개다.** 서버 손절은 있다. 휴장·주말에는 지수가 멈추고, reduce-only 제한이 걸릴 수 있고, 장 재개 때 갭과 IOC 미체결이 생길 수 있다. 그래서 휴장 중 보호는 **문서로 확인되지 않았다.**

## 4. 대체 과거 데이터 (04 3.6 2.4, 후보만, 라이선스·가격 미확인)

| 대상 | 후보 | 메모 |
|---|---|---|
| 미국 주식·ETF | 무료 일봉(Stooq 등), 유료 API(Tiingo, Polygon 계열), 생존 편향 없는 유료 DB(Norgate 등) | 무료 소스는 상장 폐지 종목이 빠지기 쉽다(생존 편향). 수정 주가 방식, 라이선스, 재배포 조건 확인 필요 |
| 한국 주식 | KRX 정보데이터시스템 / 공공데이터 API | 삼성전자 등 7종목은 장기 일봉 확보가 쉬울 것으로 보인다. 확인 필요 |
| 홍콩·중국 | 거래소·데이터 업체 | 확인 필요 |
| 원자재·외환 | 선물 연속 가격, 중앙은행·FRED 환율 | 원자재 지수는 근월물 롤오버(5~10번째 영업일) 방식이 Bybit와 다르면 차이가 생긴다 |

공통으로 놓치는 것은 펀딩, 야간·주말 무기한 거래, 무기한-현물 괴리, 휴장 뒤 갭의 실제 체결이다. Bybit 실제 데이터 약 6개월로 이 차이를 잴 수는 있다. 다만 그 구간을 보정에 쓰면 판정에는 쓸 수 없다(04 3.6 3절).

## 5. 검증 방법 후보 (04 3.6 2.5, 정하지 않음)

| 후보 | 표본 | 한계 |
|---|---|---|
| (a) 기초 자산 장기 데이터로 1단계 → Bybit 구간으로 판정 | 장기(수년~수십 년) / Bybit 약 6개월 | Bybit 구간을 보정에 쓰면 판정 불가. 6개월은 일 단위 전략도 표본이 작다 |
| (b) 지금부터 쌓기 | 시간에 비례 | 수집 시작부터 1년 이상 기다려야 한다 |
| (c) 데모 모의투자를 판정 대신 | D33 최소 4주 + 거래 수 | 데모가 TradFi를 지원하는지 미확인. 체결 품질이 실거래와 다를 수 있다 |

## 6. 제안 (1차)

**보류에 가까운 조건부 진행**을 제안한다. "진행"은 전략 연구를 해도 된다는 제안일 뿐, 운영 적합 판정이 아니다.

- 주문·손절 API가 같아서 인프라 재사용 가능성은 높다. 한국·미국 대형주와 금·원유가 있다는 점도 장점이다.
- 하지만 **휴장·주말 보호(P1)가 확인되지 않았다.** reduce-only 제한, 지수 정지, 갭·IOC 미체결이 있다. 과거도 약 6개월이다. 전략을 고르기 전에 아래 세 가지를 먼저 해결해야 한다.
  1. **사용자 확인**: 앱에서 TradFi 거래 동의 화면이 열리는지(한국 거주자 이용 가능 여부). 마스터 계정에서 웹 UI로 동의하는 방식이 괜찮은지(API 동의는 출금 권한 키가 필요해 D75와 충돌).
  2. **2차 점검(수집 계획 게시 뒤)**: allowlist 심볼만으로 거래대금 분포와 정규장·휴장별 스프레드 표본을 본다(04 3.6 2.3).
  3. **데모 확인(별도 승인)**: 데모에서 TradFi 무기한 거래 가능 여부, 휴장 중 손절 트리거, reduce-only 제한, 장 재개 갭 체결을 본다.
- 휴장 갭 위험을 피하는 설계 후보(예: 정규장 안에서만 보유, 원자재·외환처럼 거의 24시간인 상품부터)는 D104에서 다룬다. 지금 정하지는 않는다.

## 출처

- [Bybit 도움말: Introduction to TradFi Perpetual Contracts](https://www.bybitglobal.com/en/help-center/article/Introduction-to-TradFi-Perpetual-Contracts)
- [Bybit 도움말: Mark Price (Perpetual and Expiry Contracts)](https://www.bybitglobal.com/en/help-center/article/Mark-Price-Calculation-Perpetual-Expiry-Contracts)
- [Bybit API: TradFi Integration](https://bybit-exchange.github.io/docs/v5/tradfi-integration)
- [Bybit API: Get Instruments Info](https://bybit-exchange.github.io/docs/v5/market/instrument)
- [Bybit API: Enums](https://bybit-exchange.github.io/docs/v5/enum)
- [Bybit API: Place Order](https://bybit-exchange.github.io/docs/v5/order/create-order)
- [Bybit API: Set Trading Stop](https://bybit-exchange.github.io/docs/v5/position/trading-stop)
- [Bybit API: Demo Trading Service](https://bybit-exchange.github.io/docs/v5/demo)
- [Bybit Learn: TradFi Perpetuals](https://www.bybit.com/en/learn/bybit-tradfi/tradfi-perpetual-contracts) (지역 제한·KYC 언급)
- 상장 공지 예: [BYDUSDT](https://announcements.bybit.com/en/article/new-listing-bydusdt-tradfi-perpetual-contract-with-up-to-25x-leverage--art0b888fe9272b/)
