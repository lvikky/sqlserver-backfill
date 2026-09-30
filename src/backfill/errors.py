class BackfillError(Exception):
    """An operator-safe error message; never include driver exception text."""


class CommitOutcomeUnknown(BackfillError):
    """The server may have committed even though its acknowledgement was lost."""
