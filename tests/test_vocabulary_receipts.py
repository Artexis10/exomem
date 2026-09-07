"""Portable authority receipt metadata contains no paths, tokens or prose."""

import copy

from exomem import graph_sync, mutation_terminal, vocabulary_receipts


def _evidence():
    return {"version": 2, "uses": [{
        "authority_use_id": "vocab-reservation-11111111-1111-4111-8111-111111111111",
        "authorities": [{"effect_index": 0,
                         "authority_id": "vocab-auth-22222222-2222-4222-8222-222222222222",
                         "action": "edge.add", "generation": 7}],
    }]}


def test_portable_terminal_and_public_projection_retain_authority_evidence():
    evidence = _evidence()
    assert vocabulary_receipts.valid_projection(evidence)
    assert graph_sync._is_receipt_terminal_projection({
        "state": "committed", "additive_authority": evidence,
    })
    terminal = mutation_terminal.committed_terminal(
        {"path": "Knowledge Base/Notes/a.md"},
        request_id="33333333-3333-4333-8333-333333333333",
        receipt_id=None, idempotency_key=None,
    )
    terminal["additive_authority"] = evidence
    assert mutation_terminal.project_terminal(terminal)["additive_authority"] == evidence


def test_receipt_projection_refuses_embedded_content_and_future_actions():
    for field, value in (("path", "private.md"), ("action", "entity.delete"), ("generation", True)):
        evidence = copy.deepcopy(_evidence())
        evidence["uses"][0]["authorities"][0][field] = value
        assert not vocabulary_receipts.valid_projection(evidence)
        assert not graph_sync._is_receipt_terminal_projection({"additive_authority": evidence})
