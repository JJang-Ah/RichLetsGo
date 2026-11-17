# M3-07 운영 준비: 정기 대조 알림과 자정 준비 상태

## 운영 관측 (사용자 제공, 2026-11-17)

- PR #74 병합 후 13:00–13:25 KST demo Trader 확인: 시작 점검, private WS, Telegram 정상. `boot incomplete` 없음, 주문 0건, 수동 정지 유지.
- PC 시계가 약 +1.2초/일 빨라져 사용자가 로컬 `clock_limit_ms`를 5000으로 설정했다. REST 요청 타임스탬프는 offset 보정을 사용한다. 이는 M3-07에서 추적할 해당 PC 운영 값이며, 저장소 기본값이나 다른 환경의 허용 한도를 변경한 것이 아니다.
- 정기 대조가 매분 정상 완료되는데도 정지/해소/`sync` 알림이 25분 동안 58건 발생했다. 재시작 직후 준비 상태가 5분 넘게 닫힌 현상도 보고됐다. 후자는 아래 합성 경합과 부합하지만 운영 DB로 원인을 확인한 것은 아니다.

## P2-1: 진입 차단과 알림 가시성 분리

- Synchronizer는 대조 잠금을 얻으면 기존과 같은 `ENTRY_PAUSED_SYSTEM sync`를 추가하고 성공 확인 뒤 해제한다. 주문 전송/경계 직렬화, 위험 게이트, 실패 시 정지는 유지한다. 비교 시작 자체는 `OrderSender.pause()`의 원시 알림을 생성하지 않는다.
- 이번 호출이 새로 만든 sync 정지만 비교 중 알림에서 제외한다. 이미 존재하던 정지, 실패/중단 후 남은 정지는 보인다. 비교가 기존 `SyncPolicy.unknown_alert_after` 이상 지속되면 진행 중이어도 보인다(현재 앱 조립 값 2분). 알림 가시성만 달라지며 상태 조회와 진입 차단은 그대로다.
- Telegram 정지 전이는 원인(kind/reason/symbol/strategy)별 WarningBatcher 고정 창을 사용한다. 창은 기존 `[trader].warning_window_seconds`다. 첫 전이는 즉시, 창 안 후속 정지/해소는 횟수·마지막 상태 요약으로 보낸다. 해소마다 창을 초기화하지 않으며 다른 원인과 합치지 않는다. 명령 응답/확인 버튼은 이 창으로 지연시키지 않는다.
- 창 상태·관측 상태·outbox를 한 UoW로 저장한다. 큐가 꽉 차면 요약을 소비하지 않고 다음 주기에 재시도한다. 재시작 후에도 창을 복원한다. 스키마 변경 없음.
- maintenance 실패 때도 `sync.alerts`를 finally에서 배출해 안정화 실패 등 상세 알림이 다음 성공 때까지 갇히지 않게 했다. 해당 이월 P3를 이번 관련 수정에 포함했다.

## P2-2: 재현과 수정

- 수정 전 합성 테스트에서 재현: funding 완료로 `funding_wait=False` → account 조회를 일시 정지 → maintenance가 이전 `tracker.day_start`를 보고 다시 True로 설정 → account가 오늘 tracker를 게시해도 True가 남아 `can_enter()`가 거짓이 됐다.
- maintenance의 날짜 비교에 따른 중복 쓰기를 제거했다. 날짜가 다르면 기존 `can_enter()` 날짜 검사가 이미 진입을 차단한다. 실제 펀딩 대기/해소는 경계 funding 단계에서만 관리한다. 경계 I/O를 기다리도록 maintenance 전체에 잠금을 추가하지 않아 보호 작업과 명령 tick을 지연시키지 않는다.
- 합성 회귀는 자정 전환과 오래된 미확인 tracker로 재시작하는 경우 모두 account 게시 사이에 maintenance를 실행한다. account 전에는 진입 거절, account 후에는 진입 가능, 이후 5분 동안 재차 차단되지 않음을 확인한다. 기존 실제 펀딩 미확인 대기 검증도 유지한다.

## 검증

- 신규 합성 7건: 자정/재시작 경합 2건, 정상 대조 25회와 진행 중 차단, 지속/실패 대조 가시성 2건, 정지/해소 반복 20회 고정 창, 큐 포화 및 notifier 재시작 후 요약 복원.
- 명령: `.venv/Scripts/python -m pytest tests/test_trader_reconcile_notifications.py -q --tb=short` — **7 passed**.
- 전체 회귀: `.venv/Scripts/python -m pytest -q --tb=short` — **1672 passed / 194.19초**. 이후 큐 포화·재시작 회귀 1건만 추가했고 최종 신규 파일 7 passed / 1.18초로 검증했다. ruff check/format, mypy(156 source files), diff 검사 통과. 기존 Starlette/httpx 사용 중단 예정 경고 1건. 지원 Python 3.12/3.14 CI는 push 뒤 별도 확인 대상이다.
- 실제 Bot/Bybit API, 자격 증명, 로컬 설정, 운영 DB, 연구/판정 자료, D103 실행 폴더·프로세스는 접근하지 않았다. 테스트는 임시 합성 DB와 자신이 만든 코루틴만 사용한다. 실제 demo 재확인과 24시간/자정 운영은 사용자·Claude의 후속 확인이다.
