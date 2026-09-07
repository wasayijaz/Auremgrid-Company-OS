from __future__ import annotations

from .client_ops_communications import ClientOperationsCommunicationsMixin
from .client_ops_health import ClientOperationsHealthMixin
from .client_ops_opportunities import ClientOperationsOpportunitiesMixin
from .client_ops_relationships import ClientOperationsRelationshipsMixin
from .client_ops_risks import ClientOperationsRisksMixin
from .client_ops_roster import ClientOperationsRosterMixin
from .client_ops_scope import ClientOperationsScopeMixin
from .client_ops_shared import ClientOperationsSharedMixin, _now


class ClientOperations(
    ClientOperationsSharedMixin,
    ClientOperationsRosterMixin,
    ClientOperationsRelationshipsMixin,
    ClientOperationsRisksMixin,
    ClientOperationsOpportunitiesMixin,
    ClientOperationsScopeMixin,
    ClientOperationsCommunicationsMixin,
    ClientOperationsHealthMixin,
):
    """Signal-first client operations over the canonical ledger."""
