"""The reviewer's acceptance set for the carry's trigger (#1463, round 3).

Forty lower-case turns that carry a pronoun or demonstrative but start a new
topic, and twenty-five follow-ups, each sent after `EARLIER`. Written by the
reviewer, not by the rule's author. Invented content only.
"""

from __future__ import annotations

EARLIER: tuple[dict[str, str], ...] = (
    {"role": "user", "text": "we need to plan the spring rounds of the marlow quay survey"},
    {"role": "assistant", "text": "sure, two rounds are left."},
)

NEGATIVES: tuple[str, ...] = (
    'would it make sense to move the standup to thursdays',
    'is it worth upgrading my laptop ram',
    'it looks like the build is broken on main',
    'it turns out the printer was just out of paper',
    'i think that we should order pizza tonight',
    'i know that the train is late, no worries',
    'make sure that the tests pass before you push',
    'the recipe that my sister sent me needs eggs',
    'did you see the email that came in from the bank',
    'this week has been exhausting',
    'these days i mostly cook at home',
    'last year we went to portugal',
    'earlier today i fixed the boiler',
    'i finally bought a new bike and i love it',
    'can you write a haiku about autumn and make it rhyme',
    'open the terminal and run it again with sudo',
    'grab the latest docker image and tag it as stable',
    'how do i convert a pdf to png and keep it sharp',
    "thanks, that's everything for now",
    "ok that's all, cheers",
    "it's raining again",
    "it's been a long day",
    "it doesn't matter, skip it",
    'what time is it in tokyo',
    "that said, let's switch to planning the garden",
    "i'd rather do this on my own tonight",
    'is it normal for a sourdough starter to smell like vinegar',
    'it would be nice to learn some spanish this summer',
    'could it be that the router needs a reboot',
    'this is a new topic: how do i descale a kettle',
    "whatever it takes, let's get the release out",
    "that's a good point, anyway what's a good pasta recipe",
    "if it rains tomorrow we'll stay in",
    'i bought a plant and forgot to water it',
    'write a python script that renames files and run it',
    'is it cheaper to take the bus or the train to leeds',
    "it's fine, never mind",
    'just do it the usual way',
    'does it snow much in oslo in march',
    "let me know when it's done compiling",
)

POSITIVES: tuple[str, ...] = (
    'is that still on track?',
    'who owns it now',
    'what did she say about the budget',
    'can you remind me what they decided',
    'how does that compare with last year',
    'when is it due',
    "what's its current status",
    'which one did we pick',
    'the second option sounds better',
    'did he ever reply',
    'what are the risks with that',
    'is it still blocked',
    'and their deadline?',
    'remind me why we chose it',
    'what did we decide about those',
    'does this affect the rota',
    'how much did it cost in the end',
    'who else is involved in that',
    'has anything changed there since',
    "what's left to do on it",
    "it's still on track for the autumn?",
    "that's what i meant, the survey",
    'any update on them',
    'is this the latest version',
    'what was the outcome of that',
)
