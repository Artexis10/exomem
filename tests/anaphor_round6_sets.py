"""Round-six tuning examples, authored and frozen before implementation.

The fifth reviewer set stays private; these are its verbatim disclosed turns
with newly authored contexts repeating their vocabulary. They are regressions,
not a reproduction of the reviewer's complete 72/40 set.
The 10 licence and 20 temporal variants are author probes, not independent data.
"""

from __future__ import annotations

FIFTH_NEGATIVES = (
    ('N01', 'is it raining where you are', 'Alder database migration'),
    ('N02', 'does it snow there much', 'Briar workshop launch'),
    ('N03', "it's windy outside, should i go out", 'Cobalt study protocol'),
    ('N04', 'is it foggy near you', 'Dahlia gallery visit'),
    ('N05', 'how far is it to the station', 'Elm sailing itinerary'),
    ('N07', 'is it uphill all the way home', 'Alder database migration'),
    ('N08', 'how cold is it outside', 'Briar workshop launch'),
    ('N09', "it's worth asking whether you know", 'Cobalt study protocol'),
    ('N10', 'it seems that you are right', 'Dahlia gallery visit'),
    ('N11', 'is it okay to say no', 'Elm sailing itinerary'),
    ('N13', 'it is you who should decide', 'Alder database migration'),
    ('N14', 'it was because we were late that they left', 'Briar workshop launch'),
    ('N15', 'is it important that we agree', 'Cobalt study protocol'),
    ('N19', 'this recipe looks safe; can i use it', 'Alder database migration'),
    ('N20', 'i found a new map, can you explain it', 'Briar workshop launch'),
    ('N21', 'my umbrella is broken; can you fix it', 'Cobalt study protocol'),
    ('N24', 'i got a new offer; how should i respond to it', 'Fennel subscription rollout'),
    ('N25', 'is it hard for you to say no', 'Alder database migration'),
    ('N26', 'is it okay if you say you do not know', 'Briar workshop launch'),
    ('N27', 'do you get it when people say no', 'Cobalt study protocol'),
    ('N28', 'can you hear it when i speak', 'Dahlia gallery visit'),
    ('N29', 'do you have your own team or is it just you', 'Elm sailing itinerary'),
    ('N30', 'does it cost you anything to reply', 'Fennel subscription rollout'),
    ('N31', 'is it true that you do not have feelings', 'Alder database migration'),
    ('N32', 'are you a person or do they just call you that', 'Briar workshop launch'),
    ('N33', 'can you say it again', 'Cobalt study protocol'),
    ('N34', 'can you put it another way', 'Dahlia gallery visit'),
    ('N35', 'can you make it shorter', 'Elm sailing itinerary'),
    ('N36', 'was that your final reply', 'Fennel subscription rollout'),
    ('N37', 'can you explain what you mean by that', 'Alder database migration'),
    ('N38', 'please say it in french', 'Briar workshop launch'),
    ('N41', 'tea or coffee: which is it', 'Elm sailing itinerary'),
    ('N42', 'red or blue, which one do you prefer', 'Fennel subscription rollout'),
    ('N44', 'i mean a or b; which of those is right', 'Briar workshop launch'),
    ('N45', 'yes or no: can you pick one of those', 'Cobalt study protocol'),
    ('N46', 'a cat or a dog: which one costs less', 'Dahlia gallery visit'),
    ('N49', 'what does "it depends" mean', 'Alder database migration'),
    ('N50', 'is "they are ready" correct', 'Briar workshop launch'),
    ('N52', 'what does "that will do" mean', 'Dahlia gallery visit'),
    ('N53', 'in "she owns it", who is she', 'Elm sailing itinerary'),
    ('N54', 'is "it is worth it" a complete sentence', 'Fennel subscription rollout'),
    ('N55', 'what is "its" in "its price is high"', 'Alder database migration'),
    ('N56', 'does "this is fine" sound right', 'Briar workshop launch'),
    ('N57', 'can you explain it.py', 'Cobalt study protocol'),
    ('N58', 'is it.sh safe to run', 'Dahlia gallery visit'),
    ('N59', 'what does `it` mean in this function', 'Elm sailing itinerary'),
    ('N60', 'does the test `it` need a name', 'Fennel subscription rollout'),
    ('N61', 'can you explain `it = 1`', 'Alder database migration'),
    ('N62', 'what is the file it.txt for', 'Briar workshop launch'),
    ('N69', 'can you say it otra vez', 'Cobalt study protocol'),
    ('N72', 'es regnet, is that how you say it', 'Fennel subscription rollout'),
)

FIFTH_POSITIVES = (
    ('P02', 'is it still on for friday', 'Briar workshop launch'),
    ('P03', 'is it the one from last week', 'Cobalt study protocol'),
    ('P05', 'is it in the morning', 'Elm sailing itinerary'),
    ('P06', 'is it on tuesday', 'Fennel subscription rollout'),
    ('P07', 'will it be in the afternoon', 'Alder database migration'),
    ('P08', 'is it still in the morning', 'Briar workshop launch'),
    ('P09', 'is it after the weekend', 'Cobalt study protocol'),
    ('P10', 'it is at noon', 'Dahlia gallery visit'),
    ('P11', 'is it the first on tuesday', 'Elm sailing itinerary'),
    ('P38', 'drop it', 'Briar workshop launch'),
)

def earlier(subject: str, turn: str) -> tuple[dict[str, str], ...]:
    """Name the subject and repeat all probe words in an earlier user entry."""
    return ({"role": "user", "text": f"Can we review the {subject}? Our team mentioned: {turn}"},)

# Ten variants of rule 1: repeated off-subject content never licenses a carry.
LICENCE_NEGATIVES = (
    "can you fix its zipper",
    "is it crunchy enough",
    "should we change that wallpaper",
    "can they hear the violin",
    "is this itinerary safe",
    "can you compare those melons",
    "does its perfume cost more",
    "is that origami ready",
    "can you review their handwriting",
    "is its antenna broken",
)

# Exactly 20 variants of rule 3: bare complements vs preposition heads.
STRUCTURAL_NEGATIVES = (
    "will it be nearly dawn",
    "is it the middle of next winter",
    "it was the third of april",
    "is it twenty past seven",
    "is it almost midday now",
    "was it the beginning of last month",
    "is it not saturday already",
    "it is nearly dusk",
    "is it snowy outside",
    "is it five miles to the harbour",
)
STRUCTURAL_POSITIVES = (
    "will it be at dawn",
    "is it during the winter",
    "it was on the third of april",
    "is it after seven",
    "is it by midday now",
    "was it before last month",
    "is it not for saturday",
    "it is until dusk",
    "is it from yesterday",
    "is it the second in the morning",
)
