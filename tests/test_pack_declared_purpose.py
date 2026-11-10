"""The context pack decides its neighbourhood under the purpose the search declares.

A rule that withholds a page only from a caller that declares one purpose must withhold it from
the pack's neighbourhood as it withholds it from the hits, on the offline scan reader and on the
served reader alike.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_find_relation_filter import _MARKETING_ONLY, OLD_ROADMAP, _release_twin

from exomem import commands
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.governance import principal

# One word that matches a page that links to the withheld page, and one that matches a page the
# withheld page links to, so the neighbourhood reads a link in each direction. Neither word matches
# the withheld page, so it never reaches the pack as a hit that the release gate already removes.
_QUERIES = {
    "outbound-link": "supersedes",  # the roadmap links to the withheld page
    "inbound-link": "hires",  # the withheld page links to the review
}


def _publish_link_structures(vault: Path) -> None:
    """Hold the link resolver and the inbound index as a warm server does. A server that has not
    published them yet leaves the neighbourhood out, so the pack would show nothing to leak."""
    find_module._get_query_resolver(vault)
    assert vault_module.prime_inbound_index(vault, eager=True)


def _deep_search_as_external(vault: Path, query: str, *, purpose: str | None, ask: bool) -> dict:
    """The whole reply of a deep search by an external caller, the way a connector asks for it."""
    from test_governance_egress import _external

    declared = {"purpose": purpose} if purpose is not None else {}
    with principal.request_scope(_external()):
        if ask:
            return commands.op_ask_memory(
                vault, query=query, mode="keyword", scope="kb-only", limit=15, deep=True, **declared
            )
        return commands.op_find(
            vault, query=query, mode="keyword", scope="kb-only", limit=15, pack=True, **declared
        )


def _neighbours(reply: dict) -> set[str]:
    return {item["path"] for item in reply["pack"]["neighborhood"]}


@pytest.mark.parametrize("served", [False, True], ids=["scan", "served"])
@pytest.mark.parametrize("ask", [False, True], ids=["op_find", "op_ask_memory"])
@pytest.mark.parametrize("link", list(_QUERIES))
def test_a_declared_purpose_gives_the_pack_of_a_vault_that_never_had_the_withheld_page(
    tmp_path: Path, request: pytest.FixtureRequest, link: str, ask: bool, served: bool
) -> None:
    """Catches a pack that builds its neighbourhood without the purpose the search declares: a rule
    that withholds the page only for `marketing` leaves it, with its title, in the neighbourhood of
    the pages that link to it, so a caller that declares the purpose learns that it exists.
    Undeclared, the rule withholds nothing, so the same vault lists the page, which shows the
    declared pack is not empty by accident."""
    declared, undeclared = [], []
    for withheld in (True, False):
        vault = _release_twin(tmp_path, withheld=withheld, rule_extra=_MARKETING_ONLY)
        if served:
            request.getfixturevalue("warm_managed_cell")(vault)
            _publish_link_structures(vault)
        query = _QUERIES[link]
        declared.append(_deep_search_as_external(vault, query, purpose="marketing", ask=ask))
        undeclared.append(_deep_search_as_external(vault, query, purpose=None, ask=ask))

    assert declared[0]["pack"]["packed_paths"]
    assert OLD_ROADMAP in _neighbours(undeclared[0])
    assert declared[0] == declared[1]
