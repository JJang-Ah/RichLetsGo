"""UTC JSON Lines with daily rotation and redaction before any handler sees text."""

import json
import logging
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from richletsgo.domain import Environment
from richletsgo.keystore import Secret

SENSITIVE = re.compile(
    r"(?:api[-_]?key|api[-_]?secret|secret|password|token|authorization|x-bapi-sign|signature)",
    re.I,
)
HEADER = re.compile(
    r"""(?ix)((?:x-bapi-api-key|x-bapi-sign|api[_-]?key|api[_-]?secret|secret|password|(?:bot[_-]?)?token|authorization|signature)["']?\s*[:=]\s*)(?:["'][^"']*["']|Bearer\s+[^\s,;}]+|[^\s,;}]+)"""
)


class Redactor:
    def __init__(self, secrets: Iterable[Secret] = ()) -> None:
        self.values = tuple(sorted((s.reveal() for s in secrets), key=len, reverse=True))

    def text(self, value: str) -> str:
        for secret in self.values:
            value = value.replace(secret, "***")
        # Preserve valid JSON for structured values by handling keys before encoding.
        return HEADER.sub(lambda m: m[1] + '"***"', value)

    def clean(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(k): "***" if SENSITIVE.search(str(k)) else self.clean(v)
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Secret):
            return "***"
        if value is None or isinstance(value, (int, float, bool)):
            return value
        return self.text(str(value))


class RedactionFilter(logging.Filter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redactor.text(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = self.redactor.text(
                logging.Formatter().formatException(record.exc_info)
            )
            record.exc_info = None
        if record.stack_info:
            record.stack_info = self.redactor.text(record.stack_info)
        return True


class JsonFormatter(logging.Formatter):
    def __init__(self, environment: Environment, service: str, redactor: Redactor) -> None:
        super().__init__()
        self.environment, self.service, self.redactor = environment, service, redactor

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "environment": self.environment.value,
            "service": self.service,
            "at": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "fields": getattr(record, "fields", {}),
        }
        if record.exc_text:
            payload["exception"] = record.exc_text
        if record.stack_info:
            payload["stack"] = record.stack_info
        return json.dumps(self.redactor.clean(payload), ensure_ascii=False, default=str)


class DailyHandler(TimedRotatingFileHandler):
    def getFilesToDelete(self) -> list[str]:
        paths = set(super().getFilesToDelete())
        base = Path(self.baseFilename)
        cutoff = datetime.now(UTC).date() - timedelta(days=self.backupCount)
        for path in base.parent.glob(base.name + ".????-??-??"):
            try:
                day = datetime.strptime(path.name[-10:], "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                paths.add(str(path))
        return sorted(paths)


def configure_logging(
    directory: Path,
    *,
    environment: Environment,
    service: str,
    retention_days: int,
    secrets: Iterable[Secret] = (),
) -> logging.Logger:
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]+", service)
        or type(retention_days) is not int
        or retention_days < 1
    ):
        raise ValueError("invalid service name or log retention")
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("richletsgo." + service)
    for handler in tuple(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    redactor = Redactor(secrets)
    handler = DailyHandler(
        directory / (service + ".jsonl"),
        when="midnight",
        interval=1,
        backupCount=retention_days,
        encoding="utf-8",
        utc=True,
    )
    for expired in handler.getFilesToDelete():
        Path(expired).unlink(missing_ok=True)
    handler.addFilter(RedactionFilter(redactor))
    handler.setFormatter(JsonFormatter(environment, service, redactor))
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger
