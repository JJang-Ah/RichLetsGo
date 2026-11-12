"""Schema version and state repository mapping (migration is packaged with the adapter)."""

VERSION = 1
TABLES = {
    "orders": "orders",
    "fills": "fills",
    "events": "order_events",
    "positions": "positions",
    "decisions": "decisions",
    "episodes": "episodes",
    "books": "books",
    "intents": "close_intents",
    "actions": "portfolio_actions",
    "emitted": "portfolio_action_keys",
    "paused": "portfolio_paused",
    "funding_ids": "funding_applied",
    "stop_revisions": "portfolio_symbol_state",
    "stop_times": "portfolio_stop_times",
    "pause_revisions": "portfolio_pause_revisions",
    "recoveries": "protection_recoveries",
    "fill_times": "portfolio_fill_times",
    "recovery_data": "recovery_active",
    "recovery_archive": "recovery_archive",
}
