# M1-06 V0 대응표

04 2장 표의 39행을 M1-00 담당 표 순서대로 추적한다. 모든 테스트는 `tests/v0/test_recovery_integration.py`에 있다. SimulatedExchange, 실제 위험 게이트·포트폴리오·메모리 UoW·저널을 사용한다. 장애는 FaultPlan 또는 경계 함수의 장애 주입으로 재현하며 정책 판단 자체를 성공 스텁으로 바꾸지 않는다. 실제 시간 대기는 없고 ManualClock을 사용한다.

M3·M4 연동은 아래에 별도로 표시한다. M1에서는 정책만 검증하며 실제 Oracle/텔레그램/시세/AI 연결을 검증했다고 주장하지 않는다. 거래소 매칭 중 경합·부분 체결은 테스트에서 가상 거래소의 실제 `_fill` 경계로 주입한다. D90 초과 손절 시작 상태는 거래소 내부 상태에 주입하고, 이후 전송·자동 축소·조회·장부 처리는 실제 경로를 실행한다.

| 행 | 상황 | 담당 | 통합 테스트 | 외부 연동 |
|---|---|---|---|---|
| 1 | 전송 후 응답 없음 | M1-02 상태·해소 판단, M1-05 응답 유실 주입 | `test_unknown_keeps_reservation_and_periodic_cancel_across_restart` | — |
| 2 | UNKNOWN → 부분 체결 | M1-02 | `test_unknown_partial_entry_then_cancel_retains_fill_and_attached_stop` | — |
| 3 | UNKNOWN 계속 못 찾음 | M1-02 | `test_unknown_keeps_reservation_and_periodic_cancel_across_restart` | — |
| 4 | 빈 조회 지속 후 늦은 OPEN | M1-02, M1-05 조회 지연 주입 | `test_empty_successful_lookups_preserve_unknown_until_late_open` | — |
| 5 | UNKNOWN 진입 취소 성공 | M1-02 | `test_unknown_cancel_accepted_waits_for_terminal_evidence` | — |
| 6 | 취소에 "주문 없음" | M1-02 | `test_unknown_keeps_reservation_and_periodic_cancel_across_restart` | — |
| 7 | 수동 NOT_FOUND 후 체결 | M1-02 (`confirm_not_found`) | `test_manual_absence_late_fill_corrected_through_gateway` | M3 `/resolve` 명령 |
| 8 | 종결 통지 선도착 → 다른 계약 검사 | M1-02 노출, M1-03 게이트 | `test_order_before_execution_reserves_until_booked` | — |
| 9 | 미반영 체결 지연 | M1-02 판단, M1-06 조회 실행 | `test_late_execution_times_out_then_rest_restores` | — |
| 10 | 같은 체결 WS·REST 두 번 | M1-02 | `test_duplicate_and_reversed_events_book_once` | — |
| 11 | Filled 두 번 | M1-02 | `test_cancel_race_filled_twice_books_once` | — |
| 12 | WS 체결 선도착 | M1-02 | `test_execution_before_place_response_is_not_overwritten` | — |
| 13 | CANCELLED 후 체결 | M1-02 | `test_close_ioc_retries_with_stops_retained` | — |
| 14 | 부분 체결 후 취소 | M1-02, M1-04 에피소드 | `test_unknown_partial_entry_then_cancel_retains_fill_and_attached_stop` | — |
| 15 | 진입 체결 직후 강제 종료 | M1-05 부착 손절, M1-06 | `test_entry_fill_restart_adopts_attached_stop_and_deduplicates` | — |
| 16 | 청산 시작 후 장시간 미복구 | M1-04 상태 기계, M1-06 | `test_close_intent_survives_hours_offline_with_exchange_stop` | — |
| 17 | 청산 IOC 부분 체결·거절 | M1-04, M1-05 주입 | `test_close_ioc_retries_with_stops_retained` | — |
| 18 | 청산 확정 전 손절 취소 시도 | M1-04 | `test_close_ioc_retries_with_stops_retained` | — |
| 19 | 청산·손절 동시 체결 | M1-04 | `test_two_strategy_reduction_allocates_overflow_s2_first` | — |
| 20 | 청산 후 손절 정리 | M1-04 (D58), M1-05 조정 규칙 | `test_signed_entry_exit_and_no_atomic_reversal` | M3 실제 조정 규칙 확인 |
| 21 | 숏·방향 전환 부호 | M1-04 | `test_signed_entry_exit_and_no_atomic_reversal` | — |
| 22 | 외부 감소 | M1-02 귀속 보류·확정, M1-04 배분 | `test_two_strategy_reduction_allocates_overflow_s2_first` | — |
| 23 | 개인 WS 끊김 중 체결 | M1-06 동기화, M1-05 끊김 주입 | `test_disconnected_stop_fill_recovers_without_reentry` | — |
| 24 | REST 스냅샷 직후 손절 | M1-06 | `test_rest_snapshot_stop_race_invalidates_confirmation` | — |
| 25 | 손절 부족 | M1-04, M1-06 | `test_missing_stop_restoration_and_rejected_protection_reduces` | — |
| 26 | 강제 종료 후 재시작 | M1-06 | `test_created_restart_aborts_without_signal_reentry` | M3 Windows 서비스 재시작 |
| 27 | Oracle 끊김 | M1-06 (UoW 커밋 실패 주입, 진입 차단, 저널) | `test_commit_failure_entry_not_sent_protection_is_journaled` | M3 Oracle 구현, Watchdog 알림 전송 |
| 28 | Oracle 끊김 중 보호 응답 불명 → 재시작 | M1-06 | `test_unjournaled_emergency_close_is_adopted_after_restart` | — |
| 29 | amend 2회 + cancel 1회 복구 | M1-06 | `test_commit_failure_entry_not_sent_protection_is_journaled` | — |
| 30 | `requested`만 쓰고 종료 | M1-06 | `test_requested_amend_replay_is_classification_only` | — |
| 31 | 두 계약 동시 진입 한도 | M1-03 | `test_two_symbols_share_atomic_account_limit` | — |
| 32 | 같은 봉 재전달 / AI 축소 후 다음 봉 | M1-03 (signal_id), M1-04 (에피소드) | `test_same_signal_and_ai_reduction_do_not_top_up` | — |
| 33 | 시세 공백 / 지연 봉 | M1-03 `MarketDataGuard` | `test_data_gap_policy_blocks_entry_and_releases_after_backfill` | M2 보충, M3 실제 시세 수신 |
| 34 | AI 시간 초과 / 형식 오류 / 비용 상한 | M1-04 점수 결과 정책 | `test_ai_failure_blocks_entry_but_exit_still_sends` | M4 실제 AI 호출·비용 집계 |
| 35 | 일일 한도·낙폭·연속 손실 | M1-03 | `test_risk_halt_resume_authorization_and_midnight` | — |
| 36 | 텔레그램 미등록 사용자 | M1-03 `authorize_command` | `test_risk_halt_resume_authorization_and_midnight` | M3 텔레그램 봇 |
| 37 | 부분 진입 후 손절, 남은 진입 늦은 체결 | M1-04 (D64) | `test_partial_entry_stopped_then_late_fill_stays_in_closing_episode` | — |
| 38 | 저널 부분 기록 후 긴급 전송·응답 유실 | M1-06 (D65) | `test_unjournaled_emergency_close_is_adopted_after_restart` | — |
| 39 | 거래일 경계 | M1-01 경계 함수, M1-03 | `test_risk_halt_resume_authorization_and_midnight` | — |

## 보완·회귀

- 26행 추가: `test_submitted_before_send_restart_never_resends`, `test_history_out_of_range_and_manual_pause_survive_restart`.
- 35행 연속 손실·쿨다운: `test_strategy_consecutive_loss_pause_survives_sync_and_expires`.
- D61 늦은 미발동 부착 손절: `test_late_attached_connection_does_not_duplicate_protection`.
- D63 꼬리/중간 손상·cwd/한글 경로·보관/seq: `test_journal_truncated_tail_quarantined_and_sequence_continues`, `test_journal_interior_corruption_blocks_replay`, `test_journal_path_independent_of_cwd_and_archive`.
- D65 fsync 실패: `test_journal_fsync_failure_still_sends_and_marks_uncertain`; 부분 기록·DB 장애·응답 유실의 결합은 38행 lost=True.
- D90 전체 수량 변환·0 취소: `test_trim_remaining_converts_to_signed_total_and_zero_cancels`.
- D90 amend 재생 4분기: `test_requested_amend_replay_is_classification_only`.
- D90 NEWEST/OLDEST/PREFERRED_PARENT: `test_replacement_auto_shrink_never_cancels_remaining_protection`.
- D90 새 주문·취소 요청/성공 직후 재시작과 그룹별 작업 ID: `test_replacement_create_before_cancel_restart_group_and_cancel_retry`.
- D90 UNKNOWN·재시도 소진: `test_replacement_rejections_are_bounded_and_unknown_keeps_old`.

테스트 수치·손절 가격·재시도 한도는 검증용이며 운영 기본값이 아니다. Bybit 실제 자동 축소 정책과 Partial 손절 수량 변경은 M3-07 C10으로 넘긴다.

- D90 다른 전략 보호 부족 시 기존 손절 유지: `test_replacement_does_not_cancel_when_other_strategy_lost_protection`.
- D90 보유 1 = 기존 0.5 + 신규 0.5이면 두 손절 유지: `test_replacement_equal_half_stops_are_both_retained`.
