"""Domain-level exceptions."""


class InvalidStateTransition(ValueError):
    """Raised when a workflow entity is asked to enter an illegal state."""

    def __init__(self, entity: str, current: str, target: str) -> None:
        self.entity = entity
        self.current = current
        self.target = target
        super().__init__(f"Invalid {entity} transition: {current} -> {target}")


class ConcurrentStateChange(RuntimeError):
    """Raised when persisted state changed before an update could be applied."""
