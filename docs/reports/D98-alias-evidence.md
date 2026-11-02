# D98 별칭 근거 — PR #39 재검토

검토일: 2026-11-02. 기준 메타데이터: `D98-metadata-review-v3.json`.
풀 배정·가격 이력 없이 공식 프로젝트/Bybit 자료와 수명을 대조했다. 기존 MATIC/POL·RNDR/RENDER·FTM/S는 명세 초기값을 유지한다.
아래 13건을 추가 제안한다. 링크는 tables.py의 alias_sources에도 포함되어 정식 풀 digest에 들어간다.

| 별칭 | 기존 폐지 → 신규 상장 (UTC) | 근거와 해석 |
|---|---|---|
| EOS → A | 2025-05-21T09:00:00Z → 2025-05-28T08:20:02Z | [공식 자료](https://www.vaulta.com/resources/vaulta-token-swap-a-begins-may-14) — Vaulta 공식 교환 공지: EOS 후속 A 토큰. |
| MKR → SKY | 2025-08-18T09:00:00Z → 2025-08-11T08:28:06Z | [공식 자료](https://sky.money/blog/understanding-the-sky-token) — Sky 공식 문서: MKR→SKY 업그레이드. 1:24,000 전환이며 가격·수량 환산은 구현하지 않는다. |
| KLAY → KAIA | 2024-10-28T03:00:00Z → 2024-11-01T08:24:34Z | [공식 자료](https://docs.kaia.io/misc/kaia-transition/faq-chain-transition/) — Kaia 공식 FAQ: KAIA는 KLAY의 리브랜딩된 이름/티커. |
| TOMO → VIC | 2023-11-14T09:00:00Z → 2025-03-13T07:58:08Z | [공식 자료](https://www.bybit.com/en/learn/web3/what-is-viction-crypto-vic) — Bybit 설명: TomoChain→Viction 리브랜딩과 VIC 네이티브 토큰. |
| BNX → FORM | 2025-03-17T09:00:00Z → 2025-03-19T09:56:07Z | [공식 자료](https://www.bybit.com/en/learn/web3/binaryx-crypto-to-four-meme) — Bybit 설명: BinaryX→Four, BNX→FORM 이전. |
| RON → RONIN | 2025-01-26T09:00:00Z → 2025-02-13T07:11:13Z | [공식 자료](https://blog.roninchain.com/p/breaking-news-ron-tradeable-on-binance) — Ronin 공식 발표가 RON을 네이티브 토큰으로 명시. 두 계약 fullName=ronin과 결합한 동일 자산 판정(프로젝트 토큰 변경이 아니라 거래소 표기 변경). |
| RAY → RAYDIUM | 2022-11-15T04:00:00Z → 2024-06-04T09:11:59Z | [공식 자료](https://www.bybit.com/en/learn/daily-bits/raydium-launches-new-features-with-creator-rewards) — Bybit 설명이 RAY 토큰과 RAYDIUMUSDT 계약을 연결. RAY는 토큰 티커이며 RAYDIUM은 거래소 계약 표기라는 근거. 단순 이름 유사도 판정 아님. |
| MC → BEAM | 2023-11-07T07:00:00Z → 2023-11-14T09:48:16Z | [공식 자료](https://medium.com/@onbeam/beam-token-migration-update-december-2023-acfbe22b00f7) — 프로젝트 공식 공지: Merit Circle MC의 효용과 거버넌스가 BEAM으로 이전. |
| BIT → MNT | 2023-07-04T07:00:00Z → 2023-10-02T05:40:55Z | [공식 자료](https://www.bybit.com/en/learn/altcoins/what-is-mnt-token) — Bybit 설명: BitDAO BIT의 리브랜딩 후 Mantle MNT 출시. |
| GAL → G | 2024-07-22T10:00:00Z → 2024-07-25T09:07:16Z | [공식 자료](https://www.galxe.com/blog/gal-g) — Galxe 공식 공지: GAL→G 이전(1:60), 원래 계약 가격 단위는 보존. |
| COCOS → COMBO | 2023-05-26T15:00:00Z → 2023-06-08T07:53:12Z | [공식 자료](https://medium.com/combonetwork/combo-rebranding-introducing-updated-erc-20-and-bep-20-smart-contracts-2dc447ea6e5b) — COMBO 공식 공지: Cocos-BCX 리브랜딩, 기존/신규 컨트랙트 및 1:1 교환. |
| LUNA → LUNC | 2022-05-12T10:03:00Z → 2022-09-09T00:00:01Z | [공식 자료](https://docs.terra.money/migration/exchange-migration/) — Terra 공식 거래소 이전 안내: 원래 체인의 Luna는 LUNC. 별개 신규 체인 LUNA2는 합치지 않는다. |
| UST → USTC | 2022-05-13T01:30:00Z → 2023-11-27T06:42:34Z | [공식 자료](https://docs.terra.money/migration/exchange-migration/) — Terra 공식 거래소 이전 안내: 기존 UST는 USTC. 이 항목은 별칭 동일성만 고정한다. |

MKR/SKY처럼 활성 구간이 겹치면 배수→상장 시각→심볼 규칙을 그대로 적용한다. 전환 비율은 자산 키의 동일성을 부정하지 않으며 가격 시계열을 이어 붙이거나 환산하지 않는다.

## 합치지 않은 후보

- AGIX/OCEAN/FET: 별개 자산 합병이므로 이 표에서 합치지 않는다.
- PUMP/PUMPFUN, NEIROETH/1000NEIROCTO, 밈 GME/주식 GMESTOCK: 다른 자산이며 주식은 symbolType 필터로 제외한다.
- SKYAI/SKYAI1: [새 계약 공식 상장 공지](https://announcements.bybit.com/en/article/new-listing-skyai1usdt-perpetual-contract-with-up-to-10x-leverage-bltc390d2afccc10cf8/)는 확인했지만 이전 폐지 계약과의 이전 관계를 확정하지 못했다. Claude 요청대로 별칭에 추가하지 않았다.

## 상태

TABLES_FROZEN=False 유지. Claude가 근거 및 표를 재확인한 뒤에만 확정/병합 및 정식 풀 실행을 진행한다.
