"""Exact production operation-action ownership sets."""

from .models import OperationAction

DELETION_OPERATION_ACTIONS = frozenset({OperationAction.DISCARD, OperationAction.DESTROY})
DURABILITY_OPERATION_ACTIONS = frozenset(
    {
        OperationAction.EXPORT,
        OperationAction.RESTORE,
        OperationAction.EXPORT_RELEASE,
        OperationAction.EXPORT_DOWNLOAD,
        OperationAction.EXPORT_DELETE,
    }
)
ROUTINE_OPERATION_ACTIONS = (
    frozenset(OperationAction) - DELETION_OPERATION_ACTIONS - DURABILITY_OPERATION_ACTIONS
)
