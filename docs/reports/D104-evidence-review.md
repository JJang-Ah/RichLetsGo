# D104 1단계 증빙 부록 준비 기록

2026-11-21 KST. 등록 기준 fa40cf4, 실행 C=e3a3ced1a1bf420b0b07a892011d28e5eab5f8c3. 증빙 준비 단계에서 중단했다. 가격 요청이나 연구 판정은 수행하지 않았다.

| 확인 대상 | 확인한 내용 | 남은 공백 |
|---|---|---|
| 펀딩 주기 | Bybit는 2026-08-07 16:00 UTC부터 TradFi 무기한의 자동 주기 조정을 해제하고 8시간 간격을 적용한다고 공지했다. AAPLUSDT 상장 당시 안내도 8시간이었다. | 두 공지 사이에 자동 변경이 있었는지, 있었다면 어느 세션에서 어떤 주기와 기준 시각이 유효했는지 확인하지 못했다. |
| 기업행위·중단 | D103 공지 조사에서 SPCXUSDT의 6월 10일 상장 폐지는 현재 계약의 6월 15일 재상장 전이고, 기록된 SNDKUSDT 중단은 B 이후다. | 이 두 사례를 15종목 모든 세션의 verified_clear로 확장하지 않았다. 펀딩 증빙에서 중단하여 전 종목 최종 검증은 미완료다. |

등록 달력·상장 메타데이터만 대조했다. 15종목 모두 8월 7일 전환 전 세션이 있다. 총 1,181개 중 943개가 전환 전에 시작했고, 그중 13개는 전환 시각을 세션 안에 포함한다. AAPLUSDT는 4월 28일 13:30 UTC부터 71개 세션이 전환 전에 시작했다. 943은 규칙 적용 기간을 확인해야 하는 세션 수다. 자동 조정이 실제 발생한 횟수는 확인하지 못했다.

수집을 재개하려면 종목별 자동 주기 변경·복귀의 효력 시각과 정산 위상을 확인할 수 있는 기간별 자료가 필요하다. 현재 11월 스냅샷의 주기나 향후 수집할 펀딩율 응답으로 기대 일정을 채우지 않는다. 증빙을 확보하면 이 PR에서 evidence.json·sources/와 등록된 evidence 명령의 출력을 함께 검토한다. 현재 게시할 검증 성공 해시는 없다.

근거 자료는 [Bybit TradFi 펀딩 주기 변경 공지](https://announcements.bybit.com/en/article/bybit-tradfi-perpetual-contract-funding-rate-adjustment-announcement--art02886635b821/), [Bybit 자동 주기 조정 안내](https://www.bybit.com/en/press/post/bybit-launches-dynamic-settlement-frequency-system-for-perpetual-contracts-bltcd0548e9d1b5c84a), [AAPLUSDT 상장 공지](https://announcements.bybit.com/en/article/new-listing-aaplusdt-perpetual-contract-with-up-to-10x-leverage-blt09435bef6df4b244/), [D103 중단 조사 기록](D103-halts.json)이다.
