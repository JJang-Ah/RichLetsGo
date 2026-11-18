"""D108: operator-facing text only. Never interpolate remote error bodies."""

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

KST = timezone(timedelta(hours=9))
WORDS: dict[str, str] = {}
for keys, description in (
    ("operator", "직접 정지함"),
    ("sync", "장부 대조 중"),
    ("stream bybit_stream private_ws", "거래소 실시간 연결 끊김"),
    ("bybit_startup", "시작 점검 중"),
    ("query boundary_query", "거래소 주문 내역 조회 실패"),
    (
        "history_out_of_range bybit_history_window",
        "거래소 내역 조회 범위(7일) 초과, 직접 확인 필요",
    ),
    ("data", "전략 계산용 봉 데이터 부족"),
    ("clock", "PC 시계 차이가 큼"),
    ("db storage database", "저장소 확인 실패"),
    ("journal_uncertain journal_corrupt", "주문 기록 불확실, 확인 필요"),
    ("unknown unknown_overdue", "결과를 모르는 주문 있음, 확인 필요"),
    ("unbooked pending_attribution", "장부에 아직 안 맞춘 체결 있음"),
    ("replacement_exhausted", "손절 주문 교체 실패"),
    ("actual_liquidation_buffer liquidation_price_unavailable", "청산가 여유 부족 또는 확인 불가"),
    ("bybit_account_settings", "거래소 계정 설정이 예상과 다름"),
    ("bybit_instruments bybit_unknown_instrument", "상품 정보 확인 실패"),
    (
        "bybit_cashflow_mapping bybit_execution_mapping bybit_order_mapping bybit_position_mapping bybit_wallet_mapping bybit_private_mapping",
        "거래소 응답 형식이 예상과 다름",
    ),
    ("bybit_private_reconnect bybit_public_reconnect", "거래소 연결 재시도 중"),
    ("equity_unconfirmed", "평가액 미확인"),
    ("funding_off_hour", "정시가 아닌 펀딩 정산, 직접 확인 필요"),
    ("funding_pending", "자정 펀딩 정산 대기 — 새 진입 멈춤"),
    ("missing_action_order", "보호 작업의 대상 주문 확인 필요"),
    ("storage_cleanup", "저장 기록 정리 확인 필요"),
    ("storage_resolution storage_uncertain", "저장 결과 확인 필요"),
    ("sync_unstable", "장부 대조가 완료되지 않음"),
    ("telegram_id_collision", "명령 식별자 충돌, 직접 확인 필요"),
    ("telegram_queue_overflow", "알림 대기 목록이 가득 참"),
    (
        "trader_boot trader_reconcile trader_boundary boundary_state_reconcile",
        "프로그램 장부 재확인 필요",
    ),
    ("trader_started", "프로그램 시작"),
    ("trader_stopped", "프로그램 종료"),
    ("warning_summary", "반복 경고"),
    ("health_unavailable", "프로그램 응답 없음"),
    ("alive", "프로그램 종료 중"),
    ("ready", "새 진입 준비 안 됨"),
    ("boundary_stale", "시간 봉 처리가 멈춤"),
    ("disk_low", "디스크 여유 부족"),
    ("disk_unavailable", "디스크 확인 실패"),
    ("drawdown", "최고점 대비 손실 한도 도달"),
):
    WORDS.update(dict.fromkeys(keys.split(), description))
for role in ("Trader", "Dashboard", "Watchdog"):
    WORDS["restart_limit_" + role] = role + " 재시작 한도 도달 — 자동 재시작 멈춤, 직접 확인 필요"
    WORDS["restart_state_" + role] = role + " 재시작 기록 파일 이상"

KINDS = {
    "ENTRY_PAUSED_MANUAL": "수동 정지",
    "ENTRY_PAUSED_SYSTEM": "자동 정지",
    "ENTRY_PAUSED_DAILY": "오늘 손실 한도 도달",
    "STRATEGY_PAUSED": "전략 정지",
    "HALTED": "전체 중단",
}
STATES = {
    "requested": "요청 받음",
    "confirmed": "실행함",
    "completed": "완료",
    "rejected": "거절됨",
    "failed": "실패",
    "accepted": "접수됨",
    "pending": "대기 중",
    "partial": "일부 완료",
    "expired": "만료됨",
}


def env(value: str, watch: bool = False) -> str:
    return "[" + ("데모" if value == "demo" else "실거래") + (" 감시" if watch else "") + "]"


def label(code: str) -> str:
    return WORDS.get(code.split(":")[-1], "알 수 없는 상태")


def safe(code: str) -> str:
    return code if re.fullmatch(r"[a-zA-Z0-9_:.-]{1,64}", code) else "미확인"


def stamp(raw: Any, fmt: str = "%m-%d %H:%M") -> str:
    if not raw:
        return "미확인"
    try:
        dt = raw if isinstance(raw, datetime) else datetime.fromisoformat(raw)
        return dt.astimezone(KST).strftime(fmt) if dt.tzinfo else "미확인"
    except (TypeError, ValueError, OverflowError):
        return "미확인"


def pct(value: Any) -> str:
    return "미확인" if value is None else f"{Decimal(str(value)) * 100:f}%"


def pause_name(kind: str, reason: str, symbol: str = "", strategy: str = "") -> str:
    return " ".join(
        filter(
            None,
            (KINDS.get(kind, "정지"), symbol, strategy, label(reason), f"(코드 {safe(reason)})"),
        )
    )


def pauses(rows: list[dict[str, Any]]) -> str:
    return (
        ", ".join(
            pause_name(p["kind"], p["reason"], p.get("symbol") or "", p.get("strategy") or "")
            + " ("
            + stamp(p.get("since"))
            + "부터)"
            for p in rows
        )
        or "없음"
    )


def status(s: dict[str, Any]) -> str:
    pos = (
        ", ".join(
            f"{p['strategy']} {p['symbol']} {p['qty']} · 손절가 {p.get('stop') or '미확인'}"
            for p in s.get("positions", [])
        )
        or "없음"
    )
    confirmed = s.get("equity_confirmed", True)
    pnl = s.get("daily_pnl") if confirmed else None
    start = stamp(s.get("day_start")) if confirmed else "미확인"
    return f"포지션: {pos}\n오늘 손익: {str(pnl) + ' USDT' if pnl is not None else '미확인'}\n손익 기준일 시작: {start} KST (마지막으로 확정한 거래일)\n한도 사용: {pct(s.get('limit_usage'))}\n걸려 있는 멈춤: {pauses(s.get('pauses', []))}\n마지막 확정 봉: {stamp(s.get('last_boundary'))} KST"


def summary(environment: str, s: dict[str, Any]) -> str:
    return f"📊 {env(environment)} {stamp(s.get('day'), '%m월 %d일')} 일일 요약\n손익 {s.get('pnl_d81', '미확인')} USDT · 체결 {s.get('fills', '미확인')}건 · 한도 사용 {pct(s.get('limit_usage'))}\nAI 비용: 꺼짐\n걸려 있는 멈춤: {pauses(s.get('warnings', []))}"


def watchdog(
    environment: str, cause: str, recovered: bool = False, minutes: int = 0, repeat: bool = False
) -> str:
    what = (
        {
            "health_unavailable": "프로그램 응답 복구",
            "private_ws": "거래소 실시간 연결 복구",
            "ready": "새 진입 준비 완료",
            "boundary_stale": "봉 처리 재개",
            "database": "저장소 정상",
            "disk_low": "디스크 정상",
            "disk_unavailable": "디스크 정상",
        }.get(cause, "정상")
        if recovered
        else label(cause)
    )
    elapsed = "" if recovered or not minutes else f" ({'계속됨, ' if repeat else ''}{minutes}분째)"
    return f"{'🟢' if recovered else '🚨'} {env(environment, True)} {what}{elapsed} (코드 {safe(cause)})"


STARTUP = {"trader_started", "sync", "bybit_startup", "stream", "bybit_stream"}
WORDS.update(
    {
        "ALERT": "포지션 보호 상태 확인 필요",
        "PAUSE": "포지션 보호 작업 정지",
        "L4": "전략 손실 한도 도달",
        "L5": "오늘 손실 한도 도달",
        "L6": "최고점 대비 손실 한도 도달",
        "L7": "연속 손실로 전략 정지",
    }
)


def warning(environment: str, n: dict[str, Any]) -> str:
    code = n["code"]
    repeat = re.search(r" \(\d+(?:분|초) 동안 \d+번\)$", n["message"])
    icon = (
        "🟢"
        if n.get("resolved")
        else "🚨"
        if code.startswith(("storage", "portfolio:", "protection:"))
        else "⚠️"
    )
    desc = label(code)
    if code == "funding_pending":
        if n.get("resolved"):
            desc = "펀딩 정산 확인"
        else:
            icon = "💰"
    if code == "trader_stopped":
        icon = "▶️"
    return f"{icon} {env(environment)} {desc} {n.get('symbol', '')} (코드 {safe(code)})".replace(
        "  ", " "
    ) + (repeat[0] if repeat else "")


def duration(raw: str, now: datetime) -> str:
    try:
        seconds = max(0, int((now - datetime.fromisoformat(raw)).total_seconds()))
    except (ValueError, TypeError):
        return ""
    return f" ({seconds // 60}분 {seconds % 60}초)"


def resume_fields(values: dict[str, Any]) -> str:
    fields = {
        "record_peak": "기록용 최고점",
        "risk_peak": "위험 기준 최고점",
        "equity": "평가액",
        "drawdown_ratio": "낙폭",
        "new_risk_peak": "새 위험 기준 최고점",
        "next_halt_equity": "다음 정지 수준",
        "equity_tolerance_ratio": "허용 폭",
    }
    return "".join(
        f"\n{label}: "
        + (
            pct(values[k])
            if k.endswith("ratio")
            else str(values[k]) + " USDT"
            if values[k] is not None
            else "해당 없음"
        )
        for k, label in fields.items()
        if k in values
    )


WORDS.update(
    {"control_state_reconcile": "제어 명령 상태 대조 필요", "end_of_range": "검증 기간 종료"}
)
STATES.update(
    {"confirmation_required": "확인 필요", "confirming": "결과 확인 중", "interrupted": "처리 중단"}
)
