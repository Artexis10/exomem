"""Held-out turns for the conversation carry's trigger (#1463, round 3).

Written and committed BEFORE the content gate was implemented, and never
tuned against: a later rule change must meet the bar on these as they stand.
Every turn is lower case, as the owner types. Invented content only.

`EARLIER` is the conversation every turn is sent with. A positive points back
at something in it; a negative carries a pronoun or demonstrative but starts
a new topic, and must never be carried.
"""

from __future__ import annotations

EARLIER: tuple[dict[str, str], ...] = (
    {"role": "user", "text": "can we go over the kestrel hiring plan before friday's review?"},
    {"role": "assistant", "text": "sure. two analyst seats are open and the recruiter sent three shortlists."},
    {"role": "user", "text": "ok, and the harbour lantern budget is over by about four thousand."},
    {"role": "assistant", "text": "noted. the signal repair line is the one that grew."},
)

POSITIVES: tuple[str, ...] = (
    "who signed it off in the end",
    "is that still the plan",
    "what did they say about the seats",
    "when are those due",
    "how bad is it now",
    "can you remind me why it grew",
    "which one of them is more urgent",
    "did she approve that",
    "what's the latest on it",
    "are they still open",
    "should we push it to next month",
    "and the shortlists, any news on those?",
    "how does that compare with last quarter",
    "is it still over",
    "who's handling that",
    "what happens if we can't close it",
    "have we heard back from them",
    "what did we agree on that",
    "does that change the friday review",
    "and their numbers?",
    "is this blocking anything",
    "what was the reason for that",
)

NEGATIVES: tuple[str, ...] = (
    "is it safe to eat eggs past the date on the box",
    "it's freezing outside, should i wear gloves",
    "that film last night was brilliant",
    "this pasta sauce needs more salt",
    "can you translate this sentence into french for me",
    "i left my umbrella on the bus and they never found it",
    "is it too late to plant tulip bulbs",
    "my phone battery dies fast, how do i fix that",
    "they say coffee helps with headaches, is that true",
    "what's the capital of canada, i always forget it",
    "it snowed overnight so the school is closed",
    "this song is stuck in my head all day",
    "i think that my cat is overweight",
    "these shoes hurt, should i return them",
    "it takes ages for the kettle to boil",
    "can you explain how compound interest works, i never got it",
    "that reminds me, i need to call the dentist",
    "is it rude to leave a party early",
    "my brother said that the museum is closed on mondays",
    "it would be great to visit japan one day",
    "how long should i let the dough rise before baking it",
    "this weekend we're painting the kitchen",
    "i dropped my glasses and now they're scratched",
    "is that mushroom poisonous",
    "why does my laptop fan get so loud when it's hot",
    "summarise this article about sleep for me",
    "it's my mum's birthday, what should i get her",
    "these tomatoes taste bland, what am i doing wrong",
    "the neighbours are loud again, how do i ask them to stop",
    "can you check this email for typos before i send it",
    "that bakery closed down, where else sells good bread",
    "is it normal for a dog to eat grass",
)
