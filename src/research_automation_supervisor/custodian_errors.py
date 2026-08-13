"""Errors owned by the PA-5C4 Custodian and qualified ingress boundary."""

from research_automation_supervisor.errors import SupervisorError


class CustodianError(SupervisorError):
    """Base class for operator-safe Campaign Custodian failures."""


class CustodianInputError(CustodianError):
    """A wizard input, public campaign handle, or response is invalid."""


class CustodianStateError(CustodianError):
    """Custodian or operator-exchange state is inconsistent."""


class CustodianEnvironmentError(CustodianError):
    """The local environment needs an operator action before launch."""


class QualifiedCampaignError(SupervisorError):
    """Base class for the qualified Custodian-to-core entrypoint."""


class QualifiedCampaignInputError(QualifiedCampaignError):
    """A frozen campaign bundle or qualified operation is invalid."""


class QualifiedCampaignStateError(QualifiedCampaignError):
    """Qualified campaign authority or verified evidence is inconsistent."""
