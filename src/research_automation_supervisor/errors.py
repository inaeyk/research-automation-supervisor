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


class CodexConfidentialityError(CodexRequestError):
    """An exact request structure would be modified by mandatory redaction."""


class CodexDependencyError(CodexAdapterError):
    """A required local executable is missing or unusable."""


class WorkflowError(SupervisorError):
    """Base class for expected Stage 2 workflow errors."""


class WorkflowInputError(WorkflowError):
    """A substage specification, path, or workflow command is invalid."""


class WorkflowDependencyError(WorkflowError):
    """A required local workflow dependency is missing or unusable."""


class WorkflowStateError(WorkflowError):
    """Durable workflow state is unreadable or violates an invariant."""


class WorkflowLockError(WorkflowError):
    """A workflow run cannot be locked safely for mutation."""


class ShadowError(SupervisorError):
    """Base class for expected Stage 3 shadow-calibration errors."""


class ShadowInputError(ShadowError):
    """A shadow specification, review, path, or command is invalid."""


class ShadowDependencyError(ShadowError):
    """A required local shadow-calibration dependency is unavailable."""


class ShadowStateError(ShadowError):
    """Durable shadow-calibration evidence violates an invariant."""


class ShadowIntegrityError(ShadowStateError):
    """Trusted Stage 2/3 durable evidence was replaced, corrupted, or drifted."""


class ShadowConfidentialityError(ShadowInputError):
    """A Stage 3 value would be changed by the mandatory redaction policy."""


class ShadowLockError(ShadowError):
    """A shadow-calibration run cannot be locked safely."""


class LiveShadowError(SupervisorError):
    """Base class for expected Stage 4 live-shadow errors."""


class LiveShadowInputError(LiveShadowError):
    """A Stage 4 specification, review, path, or command is invalid."""


class LiveShadowDependencyError(LiveShadowError):
    """A required local Stage 4 dependency is unavailable."""


class LiveShadowStateError(LiveShadowError):
    """Durable Stage 4 evidence violates an invariant."""


class LiveShadowIntegrityError(LiveShadowStateError):
    """Trusted Stage 4 or authoritative Stage 2 evidence failed integrity checks."""


class LiveShadowRuntimeHomeInstabilityError(LiveShadowIntegrityError):
    """The persistent runtime-home namespace did not stabilize after retries."""


class LiveShadowLockError(LiveShadowError):
    """A live-shadow run cannot be locked safely."""
