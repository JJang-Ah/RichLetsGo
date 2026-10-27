"""Configuration, secret boundaries and UTC operational logging, without real credentials."""

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest

from richletsgo.config import ConfigError, load_config
from richletsgo.domain import Environment, StrategyId
from richletsgo.keystore import CredentialError, KeyStore, Secret
from richletsgo.ops.logging import configure_logging

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"


def config_file(tmp_path, old="", new=""):
    path = tmp_path / "설정 폴더" / "settings.toml"
    path.parent.mkdir(exist_ok=True)
    source = EXAMPLE.read_text(encoding="utf-8")
    path.write_text(source.replace(old, new) if old else source, encoding="utf-8")
    return path


def test_example_paths_immutability_hash_and_demo(tmp_path, monkeypatch):
    path = config_file(tmp_path)
    first = load_config(path)
    monkeypatch.chdir(tmp_path.parent)
    assert first == load_config(path)
    assert first.paths.log_dir == path.parent / "logs/backtest"
    assert json.loads(json.dumps(first.public_dict()))["environment"] == "backtest"
    with pytest.raises(FrozenInstanceError):
        first.ai_mode = "enabled"
    with pytest.raises(TypeError):
        first.keystore["bybit_demo_api"] = "another"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'environment = "backtest"', 'environment = "demo"'
        ),
        encoding="utf-8",
    )
    demo = load_config(path)
    assert demo.environment is Environment.DEMO and demo.oracle.schema == "RLG_DEMO"
    assert demo.config_hash != first.config_hash
    assert demo.strategies[StrategyId.S1].source == "manual"


@pytest.mark.parametrize(
    "old,new",
    [
        ('environment = "backtest"', 'environment = "live"'),
        ("ws_ping_seconds = 20", ""),
        ('risk_per_trade = "0.01"', "risk_per_trade = 0.01"),
        ('journal_dir = "data/demo/journal"', 'journal_dir = "data/backtest"'),
        ("working_set_days = 7", "working_set_days = 6"),
        ('candidate = "S1-A"', 'candidate = "S1-C"'),
        ("fast = 5, slow = 20", "fast = 30, slow = 20"),
        ("params = { fast = 5", "params = { faster = 5"),
        ('source = "manual"', 'source = "m2-04:bad"'),
        ('source = "manual"', ""),
        ('mode = "disabled"', 'mode = "enabled"'),
        ("https://api-demo.bybit.com", "https://api.bybit.com"),
        ('bybit_live_api = "richletsgo/bybit-live"', 'bybit_live_api = "richletsgo/bybit-demo"'),
        ('dsn = "localhost:1521/FREEPDB1"', 'dsn = "user/secret@localhost"'),
        ("[risk]", '[risk]\napi_secret = "must-not-leak"'),
    ],
)
def test_invalid_inputs_fail_closed_without_value_leak(tmp_path, old, new):
    with pytest.raises(ConfigError) as error:
        load_config(config_file(tmp_path, old, new))
    assert "must-not-leak" not in str(error.value)
    assert "user/secret@localhost" not in str(error.value)


class FakeBackend:
    def __init__(self):
        self.values = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, password):
        self.values[service, username] = password


def test_credentials_fake_backend_missing_and_repr():
    store = KeyStore(FakeBackend())
    with pytest.raises(CredentialError, match="missing credential item: demo"):
        store.get("demo")
    store.set("demo", Secret('{"key":"private-key-value","secret":"private-secret-value"}'))
    credential = store.api("demo", datetime(2026, 1, 1, tzinfo=UTC))
    assert credential.key.reveal() == "private-key-value"
    assert "private-" not in repr(credential) and str(credential.secret) == "***"
    store.set("bad", Secret('{"key":"private-key-value","secret":123}'))
    with pytest.raises(CredentialError) as error:
        store.api("bad", credential.created_at)
    assert "private-key-value" not in str(error.value)


def test_backend_error_is_sanitized():
    class Broken(FakeBackend):
        def get_password(self, *args):
            raise RuntimeError("private-value")

        def set_password(self, *args):
            raise RuntimeError("private-value")

    store = KeyStore(Broken())
    for operation in (
        lambda: store.get("demo"),
        lambda: store.set("demo", Secret("private-value")),
    ):
        with pytest.raises(CredentialError) as error:
            operation()
        assert "private-value" not in str(error.value)
        assert error.value.__suppress_context__


def test_json_logs_redact_nested_headers_text_and_exception(tmp_path):
    logger = configure_logging(
        tmp_path,
        environment=Environment.DEMO,
        service="Trader",
        retention_days=2,
        secrets=[Secret("known-private-value")],
    )
    try:
        raise ValueError("known-private-value X-BAPI-SIGN: signature-value")
    except ValueError:
        logger.exception(
            "key=%s api_key='unknown-api-value' Authorization: Bearer private-bearer",
            "known-private-value",
            extra={
                "fields": {
                    "X-BAPI-API-KEY": "nested-key",
                    "deep": [{"token": "nested-token"}],
                    "safe": 123,
                }
            },
        )
    for handler in logger.handlers:
        handler.flush()
        assert handler.utc and handler.backupCount == 2
    content = (tmp_path / "Trader.jsonl").read_text(encoding="utf-8")
    for secret in [
        "known-private-value",
        "signature-value",
        "unknown-api-value",
        "private-bearer",
        "nested-key",
        "nested-token",
    ]:
        assert secret not in content
    row = json.loads(content)
    assert row["environment"] == "demo" and row["service"] == "Trader"
    assert datetime.fromisoformat(row["at"]).tzinfo == UTC
    assert row["fields"]["safe"] == 123 and row["fields"]["deep"][0]["token"] == "***"
    assert not logger.propagate
    for handler in tuple(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def test_retention_removes_only_expired_service_files(tmp_path):
    (tmp_path / "Trader.jsonl.2000-01-01").write_text("old")
    other = tmp_path / "Other.jsonl.2000-01-01"
    other.write_text("unrelated")
    logger = configure_logging(
        tmp_path, environment=Environment.DEMO, service="Trader", retention_days=2
    )
    assert not (tmp_path / "Trader.jsonl.2000-01-01").exists()
    assert other.exists()
    for handler in tuple(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def v1_fixture(tmp_path, mutate=None):
    import tomllib

    from richletsgo.backtest.codec import digest

    path = config_file(tmp_path)
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    pair = [config["strategy"][s]["params"] for s in ("S1", "S2")]
    identity = {"preregistration": {"id": "synthetic", "sha256": "a" * 64}}
    body = {
        "identity": identity,
        "manifest_hash": digest(identity),
        "aggregates": {"S1A-S2A": {"verdict": "PASS"}},
        "windows": {"S1A-S2A": [{"status": "COMPLETED", "selected": pair}]},
    }
    if mutate:
        mutate(body)
    report = path.parent / "보고서.json"
    report.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'source = "manual"', f'source = "v1:{digest(body)}"'
        )
        + '\n[strategy]\nreport_path = "보고서.json"\n',
        encoding="utf-8",
    )
    return path, report


def test_v1_report_digest_pair_and_relative_path(tmp_path, monkeypatch):
    path, report = v1_fixture(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    cfg = load_config(path)
    assert cfg.report_path == report
    # Whitespace is irrelevant to canonical body hashing.
    report.write_text(json.dumps(json.loads(report.read_text(encoding="utf-8"))), encoding="utf-8")
    assert load_config(path) == cfg
    body = json.loads(report.read_text(encoding="utf-8"))
    body["aggregates"]["S1A-S2A"]["verdict"] = "FAIL"
    report.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ConfigError, match="digest"):
        load_config(path)


@pytest.mark.parametrize(
    "kind",
    [
        "fail",
        "mixed_windows",
        "incomplete",
        "manifest",
        "registration",
        "missing_file",
        "mixed_source",
        "legacy",
    ],
)
def test_v1_fail_closed(tmp_path, kind):
    def mutate(body):
        if kind == "fail":
            body["aggregates"]["S1A-S2A"]["verdict"] = "FAIL"
        elif kind == "mixed_windows":
            import copy

            first = body["windows"]["S1A-S2A"][0]
            second = copy.deepcopy(first)
            first["selected"][0]["fast"] = 6
            second["selected"][1]["period"] = 30
            body["windows"]["S1A-S2A"].append(second)
        elif kind == "incomplete":
            body["windows"]["S1A-S2A"][0]["status"] = "NO_CANDIDATE"
        elif kind == "manifest":
            body["manifest_hash"] = "f" * 64
        elif kind == "registration":
            body["identity"]["preregistration"] = None
            from richletsgo.backtest.codec import digest

            body["manifest_hash"] = digest(body["identity"])

    path, report = v1_fixture(tmp_path, mutate)
    if kind == "missing_file":
        report.unlink()
    if kind in ("mixed_source", "legacy"):
        import re

        source = path.read_text(encoding="utf-8")
        source = re.sub(
            r'source = "v1:[0-9a-f]{64}"',
            'source = "manual"' if kind == "mixed_source" else 'source = "m2-04:' + "a" * 64 + '"',
            source,
            count=1,
        )
        path.write_text(source, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_s2p_config_validates_parameters(tmp_path):
    path = config_file(tmp_path)
    source = path.read_text(encoding="utf-8").replace('candidate = "S2-A"', 'candidate = "S2-P"')
    source = source.replace(
        'params = { period = 20, k = "2", atr_period = 14, atr_multiple = "2", max_hold_bars = 24 }',
        'params = { rsi_period = 2, lower = "20", atr_period = 14, stop_atr = "1.5", target_r = "2", max_hold_bars = 30 }',
    )
    path.write_text(source, encoding="utf-8")
    assert load_config(path).strategies[StrategyId.S2].candidate == "S2-P"
