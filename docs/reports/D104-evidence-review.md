# D104 1단계 증빙 부록

2026-11-21 KST 사용자 결정 (a)를 적용했다. 등록 코드 C는 `c45c18f842ed3b1f4761861527951ee6d9639748`이다. [evidence.json](D104-stage1-evidence/evidence.json)과 [evidence 명령 출력](D104-stage1-evidence-output.json)을 게시한다. 검증 성공 해시는 `24c9559ad4e07f52c068e8f3e24fc2cf4e1498471810b6104544bfe325d6e287`이다.

| 보존 자료 | 적용 범위와 원문 |
|---|---|
| [펀딩 공지 본문](D104-stage1-evidence/sources/89ce07ba511175c89633312ee50d284e636f82b68539331f1d2e640fedffd34d) | Bybit 영문 공지 HTML의 article-detail-content 요소 내부 바이트를 줄바꿈·문자 정규화 없이 보존했다. 2026-08-07 16:00 UTC부터 8시간 주기와 자동 주기 조정 해제를 명시한다. [공식 공지](https://announcements.bybit.com/en/article/bybit-tradfi-perpetual-contract-funding-rate-adjustment-announcement--art02886635b821/) |
| [중단 조사 원문](D104-stage1-evidence/sources/13a8c54ba5e88eebd6dfff8571563adbe5963f1de924400e4d91c50335debeaa) | D103-halts.json의 보존 바이트. 중단 표와 sources_checked에서 확인한 SPCXUSDT의 pre-IPO 상장폐지~재상장 구간을 대조했다. 이 구간은 현재 계약 세션 시작 전이며 SNDKUSDT의 기록된 사건은 B 뒤다. [조사 원본](D103-halts.json) |

15종목·1,181세션 중 전환 전에 시작하는 세션은 943개이고 그중 13개는 전환을 가로지른다. 전환 전 API가 누락한 정산과 공지되지 않은 일시 중단은 독립적으로 입증할 수 없다. 결과에 두 가정을 표시한다. 증빙 검사는 가격·펀딩율 요청 없이 종료했다.
