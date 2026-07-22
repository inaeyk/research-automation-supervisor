"""Domain errors exposed by the supervisor."""


class SupervisorError(Exception):
    """Base class for expected, user-facing supervisor errors."""


class ContractError(SupervisorError):
    """Base class for errors encountered while loading a stage contract."""


class ContractLoadError(ContractError):
    """A contract could not be read or parsed as YAML."""


class ContractValidationError(ContractError):
    """Parsed contract data did not satisfy the contract schema."""


class CodexAdapterError(SupervisorError):
    """Base class for expected errors from the deterministic Codex adapter."""


class CodexRequestError(CodexAdapterError):
    """A Codex request or one of its referenced inputs is invalid."""


class CodexDependencyError(CodexAdapterError):
    """A required local executable is missing or unusable."""
