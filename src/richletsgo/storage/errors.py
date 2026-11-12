"""Persistence failures distinguished by whether retry is safe."""


class StorageError(OSError):
    pass


class StorageBusy(StorageError):
    """No commit was submitted; the same input may be retried."""


class DuplicateExecId(ValueError):
    pass


class DuplicateSignalId(ValueError):
    pass


class CommitOutcomeUnknown(StorageError):
    def __init__(self, txn_id: str) -> None:
        self.txn_id = txn_id
        super().__init__(f"commit outcome unknown: {txn_id}")
