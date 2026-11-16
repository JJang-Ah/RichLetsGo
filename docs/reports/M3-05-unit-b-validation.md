# M3-05 단위 (b) 검증 기록

- 기준: PR #73 단위 (a) ce8a3ec 검토 통과, I1~I7·Q1~Q4·C1/C2 승인.
- 범위: Telegram 본 작업과 승인된 P3 두 건. 실제 봇 수동 확인은 미실행이며 사용자와 함께 별도로 수행한다.
- 실제 자격 증명/계좌/Bot API/연구 DB/판정 자료/D103 실행 폴더·프로세스에 접근하지 않았다. 테스트는 가짜 Bot API·가짜 시계·가상 거래소·임시 DB만 사용했다.

## 구현과 검증 대조

| 계약 | 구현·회귀 |
|---|---|
| I1 저장·봇 소유권 | 기존 V001/V002 유지, V003 불필요. 공개 bot_id와 환경/계좌로 namespace를 고정하고 다른 봇 커서 재사용 거부. 토큰은 KeyStore→BotClient 메모리 주입만. HTTP 리다이렉트 금지/에러 본문·URL 비노출 |
| I2 수신·멱등 | inbox/offset 같은 UoW 후 다음 getUpdates 확인. 로컬 여유만큼 limit을 요청하며 저장 실패 시 offset 미전진. 재시작/24시간 무수신 offset 생략 조회, ID/내용 충돌 시 실행 보류·진단. 원래 update_id로 CommandService/V002 멱등. 미확정 확인 요청 재발급 금지, 이미 확정된 취소 결과 재생 |
| I3 공통 명령 | 권한/거절 감사/status는 공통 서비스, 변경 명령은 기존 request/ResumeService/tick 재사용. 웹 응답 JSON 전체 방송 제거. 개인 채팅/허용 사용자만 요청·응답, 그룹/미등록 사용자 무응답·감사. 기존 TelegramCommands에도 개인 채팅 제한 |
| I4 확인 | 60초 nonce·요청자/채팅/봇 namespace/원래 문맥/메시지 ID 바인딩. 다른 사용자·메시지·채팅·만료·재시작 전 번호 거절. 같은 콜백 재수신은 기존 결과. UNKNOWN→NOT_FOUND만 지원하고 손절을 cancel 대상에서 제외. 빈 callback ACK는 권한 정보를 노출하지 않음 |
| I5 묶음·P3 | 생산자가 Notice(code/symbol/strategy/related/resolved)를 직접 전달. app의 문자열 분할 제거. 로그와 영속 Bot 묶음이 WarningBatcher를 공유하며 resolve로 해소 이벤트 처리. 첫 경고/고유 체결 보존, 반복 횟수 요약, 긴급 첫 경고·명령/정지 응답을 일반 요약보다 우선. 정시 아닌 펀딩에 **수동 확인 필요** 명시 |
| I6 실패 격리 | 수신/처리/발송 supervisor와 Bot task를 Trader 필수 작업 종료 조건에서 분리. CommandService 알림은 outbox enqueue만 수행. 가짜 sendMessage를 막은 상태에서 실제 Trader 경계 신호 4개 ADMITTED 확인. 401/403/5xx/비정상 JSON·429/전역 retry_after·전송 간격·큐 상한/탈락 수 진단 검사 |
| 발송 결과 불명 | HTTP 전 시도를 커밋하고 성공 뒤 sent/archive. 전송 중 취소 뒤 재시도는 possible_duplicate 이력을 유지한다. Bot API 발송은 exactly-once를 주장하지 않으며 도메인 명령 멱등과 분리 |
| I7 일정 | api_expires_at UTC 필수·기본 없음, created_at보다 이후 검증. UTC 날짜 14일/13일/0일/만료 후·윤년/월말·갱신 세대별 발송. 일일 요약을 자정 계좌 커밋과 같이 저장해 펀딩 지연 중 미발송/확정 뒤 1회. D81 계좌 손익/체결 수/사용률/경고, AI 비용 **비활성** |
| C1 작업 집합 | 완료 inbox/outbox/notice/확인 receipt를 archive(active=0). SQLite 기본 UoW는 Telegram archive payload를 적재하지 않으며 지정한 키만 조회. 완료 합성 500건에서 기본 작업 집합 0건, 지정한 1건만 적재 검증. 별도의 SQL 범위 필터이며 새 색인/전체 SQL 스캔 제거로 주장하지 않음 |

## 검증 명령

- 신규 `tests/test_telegram.py`: **42 passed** (가짜 API·임시 DB만).
- 최종 Telegram/공통 명령/Trader/단위 (a) 관련: **143 passed / 10.34초**.
- 최종 전체 회귀: **1619 passed / 187.45초**. 발송 선행 기록·확정 결과 재생·빈 콜백 ACK·개인 채팅 어댑터 보완까지 포함한다.
- `ruff check .`, `ruff format --check .`, `mypy`(156 source files), diff 검사 통과. 기존 Starlette/httpx 사용 중단 예정 경고 1건 외 실패 없음.

## 보장 범위와 수동 확인

- 영속 inbox/outbox/묶음 커밋 이후 재시작을 복구한다. DB에 아직 적재하지 못한 bounded 메모리 경고 반복 횟수는 재시작 시 보장하지 않는다. 거래·명령 결과는 기존 영속 상태에서 복원하며 발송 장애가 보호 작업을 막지 않는다.
- 큐 상한은 메모리 입력/발송 outbox에 각각 적용한다. 요약 대기 상태는 배치 레코드로 보존해 꽉 찬 큐에서도 진전한다. 전송 전역 backoff는 다른 메시지에도 적용한다.
- `limit_usage`는 저장된 포지션/주문과 마지막 내부 mark가 있을 때 계산하고 없으면 미확인이다. 실제 키/운영 설정 파일을 생성하거나 만료일을 추정하지 않았다.
- 코드 검토/CI 뒤 사용자가 demo 설정·키 보관·허용 개인 채팅을 준비한 상태에서 명령 5종과 알림 1종을 함께 확인한다. 토큰/키는 채팅에 붙이거나 출력하지 않는다. 자연 발생 UNKNOWN이 없는 상태에서 `/resolve` 확인을 위해 실제 주문 상태를 조작하지 않는다. 수동 결과는 후속 기록으로 남긴다.
