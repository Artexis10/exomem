# User notes

A user note tells a person who does not write code what changed. Each release's notes show on Home in Evermatter Cloud, each as a short title that opens its full sentence. After an update, the person's assistant also tells them the newest note once.

## When to add a note

Add one note when a person using Evermatter would notice your change: something new they can do, or something that now works and did not before. A change that nobody would notice needs no note.

## How to add a note

1. Create `user-notes/<short-name>.md`. People never see the file name.
2. Start the file with front matter that holds the title, between two `---` lines.
3. Write the title in plain words, on one line, at most 48 characters, with no full stop at the end.
4. Below the front matter, write one paragraph on one line, at most 200 characters.
5. Use plain words in both. Name no module, tool, file, setting or internal term.
6. Use no Markdown in either: no bold, links, code or lists.
7. Say what the person gets, not what the code did.

Example file:

```
---
title: Batch moves and renames finish
---
Asking your assistant to move or rename a batch of pages now finishes, instead of stopping partway and repeating itself.
```

The title is about four or five words and says what the person gets. The front matter takes no other key. If the title has a colon, a `#`, or starts with a word such as Yes, On or a quote or `@`, put it in double quotes, for example `title: "Imports: faster"`. The check tells you when a title needs quotes. The assistant's one-time line after an update uses the sentence only.

## When to add no note

A `feat`, `fix` or `perf` pull request must either add a note or state that it has none. For no note, put this line in the pull-request body:

```
User note: none
```

The pull-request check reads the title type, the note files the pull request adds or changes, and that line. It checks each note's shape, including the title, and never judges its words.

## How notes reach people

The release build gives each note to the first release tag whose history contains the commit that added the file. Files stay in the tree after release, and no release step edits or deletes them. Never rename or move a note file: the build reads a renamed file as a new note and announces it again in the next release. To reword a note or its title, edit the file in place; an edit does not announce it again.
