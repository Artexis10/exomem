# User notes

A user note tells a person who does not write code what changed. Each release's notes show on Home in Evermatter Cloud. After an update, the person's assistant also tells them the newest note once.

## When to add a note

Add one note when a person using Evermatter would notice your change: something new they can do, or something that now works and did not before. A change that nobody would notice needs no note.

## How to add a note

1. Create `user-notes/<short-name>.md`. People never see the file name.
2. Write one paragraph on one line, at most 200 characters.
3. Use plain words. Name no module, tool, file, setting or internal term.
4. Use no Markdown: no bold, links, code or lists.
5. Say what the person gets, not what the code did.

Example: `Photos now keep the date they were taken, even when your chat app removed it.`

## When to add no note

A `feat`, `fix` or `perf` pull request must either add a note or state that it has none. For no note, put this line in the pull-request body:

```
User note: none
```

The pull-request check reads only the title type, the added files and that line. It checks the note's shape and never judges its words.

## How notes reach people

The release build gives each note to the first release tag whose history contains the commit that added the file. Files stay in the tree after release, and no release step edits or deletes them.
