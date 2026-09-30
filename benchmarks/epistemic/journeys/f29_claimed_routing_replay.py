"""f29: published observations belong in the existing claiming collection."""

from __future__ import annotations

import hashlib

from .collection_replay import ReplayAttachment, ReplayCorpus, ReplayTurn, build_corpus

COLLECTION_ID = "f60838d2-6049-4e8c-8045-b26c7ed8e365"


def routing_corpus() -> ReplayCorpus:
    turns: list[ReplayTurn] = []
    for index, day in enumerate(("2026-09-01", "2026-09-04", "2026-09-08"), 1):
        exact = f"Studio update {index}.\nThe new print run is ready."
        evidence = f"Knowledge Base/Evidence/studio/publication-{index}.md"
        key = f"studio-{day}-{index}"
        proof = ReplayAttachment(
            name=f"publication-{index}.txt",
            media_type="text/plain",
            content=f"Publication confirmation\nReference: {key}\nPublished: {day}\nPlatform: bulletin\nAccount: studio\n\n{exact}\n",
        )
        turns.append(
            ReplayTurn(
                f"t{index:02d}-published",
                f"Published from the studio account on bulletin on {day}, reference {key}. "
                f"The attachment publication-{index}.txt is proof and contains the final text.",
                day,
                (
                    ("post_key", key),
                    ("published_on", day),
                    ("platform", "bulletin"),
                    ("account", "studio"),
                    ("exact_text", exact),
                    ("text_sha256", hashlib.sha256(exact.encode()).hexdigest()),
                    ("sources", (evidence,)),
                ),
                attachments=(proof,),
            )
        )
        if index == 1:
            turns.append(
                ReplayTurn(
                    "t01-draft",
                    "I might publish another update tomorrow; this is only an idea.",
                    day,
                )
            )
    return build_corpus(
        ReplayCorpus(
            "f29-claimed-routing-v1",
            "f29",
            "bulletin",
            tuple(turns),
            ("post_key",),
            seeded_collection=COLLECTION_ID,
        )
    )


if __name__ == "__main__":
    from .collection_replay_driver import main

    raise SystemExit(main(routing_corpus))
