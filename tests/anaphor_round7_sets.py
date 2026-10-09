"""Round-seven variants frozen before implementation; authored regression probes."""

TOPIC_SWITCH_VARIANTS = (
    "can you compare its upholstery",
    "is that avalanche safe",
    "how did her ceramics compare",
    "should we review their greenhouse",
    "can you change this password",
    "i found a compass; can you explain it",
    "my bicycle needs a review; is it safe",
    "i got a parcel; can you check that",
    "can you explain `they = 2` again",
    "is `that` ready",
)

# Round 7 surface-only ruling also exposes asking/seems in N09/N10.
# Sixteen residuals name nothing; four are topic switches.
CONTENT_FREE_FIFTH_IDS = frozenset({
    "N11", "N13", "N14", "N15", "N26", "N27", "N29",
    "N30", "N33", "N34", "N36", "N37", "N45", "N50", "N52", "N53",
})
RESIDUAL_TOPIC_SWITCH_IDS = frozenset({"N09", "N10", "N24", "N61"})
# Content-free only under the removed task vocabulary. T2e licenses their words
# from the subject's title or one admitted unit, so without support they fall
# through (recall record under task 6.17c).
TASK_WORD_FIFTH_IDS = frozenset({"N13", "N15", "N29", "N30", "N36", "N37"})
STEM_COLLISION_WORDS = ("offer", "comer", "giver", "taker", "looker", "thinker", "knower", "asker")
CODE_TURNS = (
    "can you explain `it = 1`",
    "is `they` ready",
    "can you review ``that = 2``",
    "can you explain ```\nit = 1\n```",
    "can you check it against `1`",
    "can you explain `offer`",
)
