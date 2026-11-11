## Why

Search for a value in a small dataset matches a record only when every query word is in it. Agents and people ask in natural words, so most real phrasings miss. "Acme's IBAN" needs a word "s". "IBAN of Acme" needs the word "of". A query that names the attribute differently from the field name misses too. On a measured vault of several hundred small datasets, the current rule found a known supplier profile for 10 of 22 natural phrasings. It found 1 of 69 random facts asked as a question.

The same rule is also too loose. A single-object JSON is one record, so "every word in the record" means "every word anywhere in the file". Agent-written diagnostic JSONs hold common words such as "test", "results" and "review" in hundreds of fields. On the same vault, "test results" returned 72 value hits and "review verdict" returned 69, each with a passage of up to 4,000 characters. Value hits had equal weight with notes, so they took half of the top results.

## What Changes

- Function words and one-letter fragments of a query word are not required. A possessive "s" is an example of a fragment. Page search already uses the same function-word list.
- A record matches when one small part of it holds the query's distinctive words, and at least one of those words is in a value. A small part is the smallest object that holds the words, with at most 40 fields. The distinctive words are the words that carry at least half of the query's weight. A word's weight grows as fewer datasets hold it. A word that no dataset holds has the most weight, so a name that is not on file still prevents a match.
- In a scope with at least 40 datasets, a match is dropped when its words together occur in more than 2% of the datasets (at least 3). This keeps common words in diagnostic exports out of the results.
- At most 3 value hits enter a search result.
- A value hit's passage starts at the match and is about 600 characters. Today it holds the whole record, up to 4,000 characters.
- When value hits match equally well, the dataset with the newest card comes first. Two versions of a profile that hold the same account number are an example.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `structured-source-preservation`: the value search requirement adds the matching rule, the noise gate, the cap, the passage size and the order on ties.

## Impact

- `src/exomem/dataset_values.py`: a schema with one row for each field, the new matching and ranking, the cap and the passage. The sidecar version changes, so the first search after the upgrade rebuilds the sidecar.
- `src/exomem/find_policy.py`: no change. The value lane reuses `query_word_stem_groups`.
- No change to release decisions, the vault, cards, tool parameters or the hosted projection path.
