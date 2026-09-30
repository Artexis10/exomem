"""f28: recurring studio licence events, with a frequency-matched one-off twin."""

from __future__ import annotations

from .collection_replay import ReplayCorpus, ReplayTurn, build_corpus


def promotion_corpus(*, twin: bool = False) -> ReplayCorpus:
    identities = (
        ("Cedar", "Quartz", "Amber", "Birch") if twin else ("Cedar", "Quartz", "Cedar", "Quartz")
    )
    dates = ("2026-08-18", "2026-08-25", "2026-09-01", "2026-09-08")
    states = ("purchased",) * 4 if twin else ("purchased", "purchased", "cancelled", "refunded")
    amounts = ("24.00", "36.00", "12.00", "18.00") if twin else ("24.00", "36.00", "0.00", "36.00")
    turns: list[ReplayTurn] = []
    for index, (identity, date, status, amount) in enumerate(
        zip(identities, dates, states, amounts, strict=True), 1
    ):
        title = f"{identity} studio licence"
        text = (
            f"On {date}, the {title} was {status} for EUR {amount}. "
            "That is the confirmed result for the studio expenses."
        )
        turns.append(
            ReplayTurn(
                f"t{index:02d}-expense",
                text,
                date,
                (
                    ("identity", identity),
                    ("effective_on", date),
                    ("status", status),
                    ("amount", amount),
                    ("observed_precision", "exact"),
                    (
                        "sources",
                        (f"Knowledge Base/Notes/Research/studio/expense-{index}.md#event",),
                    ),
                ),
            )
        )
        if index == 2:
            turns.append(
                ReplayTurn(
                    "t02-tentative",
                    "The next renewal might be cheaper; nothing has changed yet.",
                    date,
                )
            )
    turns.append(
        ReplayTurn(
            "t05-elapsed",
            "It has been another week since I considered a different editor.",
            dates[-1],
        )
    )
    boundary = len(turns)
    turns.append(
        ReplayTurn(
            "t06-confirm",
            "Yes, go ahead with the proposal you just described."
            if not twin
            else "Thanks, that is all for now.",
            dates[-1],
            confirm=not twin,
        )
    )
    return build_corpus(
        ReplayCorpus(
            "f28-collection-promotion-twin-v1" if twin else "f28-collection-promotion-v1",
            "f28",
            "licences",
            tuple(turns),
            ("identity", "effective_on"),
            expect_candidate=not twin,
            candidate_turn=boundary,
        )
    )


if __name__ == "__main__":
    from .collection_replay_driver import main

    raise SystemExit(main(promotion_corpus))
