'use strict';
// Lists the commits that release-please would show in a changelog, using its own
// Conventional Commits parser and this repository's changelog sections.
// Input on stdin: a JSON array of {sha, message}. Argument: the release-please config path.
// Output: a JSON array of {sha, type, breaking, subject} for each visible entry.
const fs = require('node:fs');
const {parseConventionalCommits} = require('release-please/build/src/commit');
// release-please logs to stdout by default; send it to stderr so stdout stays one JSON document.
const toStderr = (...args) => console.error(...args);
require('release-please/build/src/util/logger').setLogger({
  info: toStderr, debug: toStderr, warn: toStderr, error: toStderr, trace: toStderr,
});

const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const sections = (config.packages?.['.'] ?? {})['changelog-sections'] ?? config['changelog-sections'];
if (!Array.isArray(sections)) {
  throw new Error('release-please config names no changelog-sections');
}
const visible = new Set(sections.filter(section => !section.hidden).map(section => section.type));
const commits = JSON.parse(fs.readFileSync(0, 'utf8')).map(({sha, message}) => ({sha, message, files: []}));
const entries = parseConventionalCommits(commits)
  .filter(entry => entry.breaking || visible.has(entry.type))
  .map(entry => ({sha: entry.sha, type: entry.type, breaking: entry.breaking, subject: entry.bareMessage}));
process.stdout.write(JSON.stringify(entries));
