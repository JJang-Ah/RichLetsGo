"""D45/D63 durable protective journal. Paths and timestamps come from callers."""

import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from richletsgo.domain.clock import require_utc


class JournalCorrupt(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class JournalRecord:
    operation_id: str
    phase: str
    seq: int
    op_type: str
    target: str
    request: str
    result: str
    ts: str
    action_key: str | None = None
    group_id: str | None = None
    step: int | None = None
    attempt: int | None = None

    def __post_init__(self) -> None:
        if not self.operation_id or self.phase not in ("requested", "result"):
            raise ValueError("invalid journal identity/phase")
        if type(self.seq) is not int or self.seq < 0:
            raise ValueError("invalid sequence")
        if self.op_type not in ("create", "close", "amend", "cancel") or not self.target:
            raise ValueError("invalid protective operation")
        for data in (self.request, self.result):
            if not isinstance(json.loads(data), dict):
                raise ValueError("journal payload must be an object")
        require_utc(datetime.fromisoformat(self.ts))
        if self.op_type in ("create", "close") and not json.loads(self.request).get("reduce_only"):
            raise ValueError("entry orders cannot use the protective journal")

    @property
    def key(self) -> str:
        return f"journal:{self.operation_id}:{self.phase}"

    def encode(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def decode(cls, value: str) -> "JournalRecord":
        return cls(**json.loads(value))

    def payload(self) -> dict[str, Any]:
        return dict(json.loads(self.request))


class ProtectiveJournal:
    """Single writer per injected account directory, one fsync per appended line.

    Every process instance starts a new segment. Archives remain available for
    audit and sequence allocation. A damaged tail is quarantined, never evidence
    that a request was not sent. Interior damage stops replay entirely.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self.archive_dir = self.directory / "archive"
        self.uncertain = False
        self.corrupt = False
        self._seq = 0
        self._path: Path | None = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            records = self.read()
            self._seq = max((r.seq for r in records), default=0)
            self._rotate()
        except JournalCorrupt:
            self.corrupt = True
            self.mark_uncertain()
        except OSError:
            self.mark_uncertain()

    def mark_uncertain(self) -> None:
        self.uncertain = True
        try:
            with (self.directory / "JOURNAL_UNCERTAIN").open("wb") as stream:
                stream.write(b"reconcile exchange before enabling entries\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            pass  # D65: even the marker can fail; reconciliation is unconditional.

    def _segments(self) -> list[Path]:
        return sorted(
            (*self.directory.glob("journal-*.jsonl"), *self.archive_dir.glob("journal-*.jsonl")),
            key=lambda p: int(p.stem.split("-")[-1]),
        )

    def _rotate(self) -> None:
        number = max((int(p.stem.split("-")[-1]) for p in self._segments()), default=0) + 1
        self._path = self.directory / f"journal-{number:08d}.jsonl"
        with self._path.open("xb"):
            pass

    def read(self) -> tuple[JournalRecord, ...]:
        records: list[JournalRecord] = []
        sequences: set[int] = set()
        identities: dict[str, JournalRecord] = {}
        for path in self._segments():
            data = path.read_bytes()
            lines = data.splitlines(keepends=True)
            offset = 0
            for index, line in enumerate(lines):
                try:
                    if not line.endswith(b"\n"):
                        raise ValueError("unterminated line")
                    record = JournalRecord.decode(line.decode("utf-8"))
                except (ValueError, TypeError, UnicodeError) as error:
                    self.mark_uncertain()
                    if index != len(lines) - 1:
                        self.corrupt = True
                        raise JournalCorrupt(f"interior corruption in {path.name}") from error
                    # Preserve the original suffix before truncation. On failure,
                    # leave the source intact and block replay.
                    partial = path.with_suffix(".partial")
                    with partial.open("ab") as saved:
                        saved.write(line)
                        saved.flush()
                        os.fsync(saved.fileno())
                    with path.open("r+b") as source:
                        source.truncate(offset)
                        source.flush()
                        os.fsync(source.fileno())
                    break
                if record.seq in sequences or record.key in identities:
                    self.corrupt = True
                    raise JournalCorrupt("duplicate journal sequence/identity")
                sequences.add(record.seq)
                identities[record.key] = record
                records.append(record)
                offset += len(line)
        return tuple(sorted(records, key=lambda r: r.seq))

    def append(self, record: JournalRecord) -> JournalRecord:
        if self.corrupt:
            raise JournalCorrupt("journal replay is blocked")
        try:
            if self._path is None:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._seq = max(self._seq, max((r.seq for r in self.read()), default=0))
                self._rotate()
            self._seq += 1
            record = replace(record, seq=self._seq)
            assert self._path is not None
            with self._path.open("ab") as stream:
                stream.write((record.encode() + "\n").encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            return record
        except OSError:
            self.mark_uncertain()
            self._path = None  # Never append after a potentially truncated tail.
            raise

    def archive(self, applied_keys: set[str]) -> None:
        """Archive only segments completely acknowledged by the DB transaction."""
        self.read()  # Validate/quarantine before moving anything.
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        for path in self.directory.glob("journal-*.jsonl"):
            if path == self._path:
                continue
            records = [
                JournalRecord.decode(line) for line in path.read_text(encoding="utf-8").splitlines()
            ]
            if all(r.key in applied_keys for r in records):
                path.rename(self.archive_dir / path.name)
