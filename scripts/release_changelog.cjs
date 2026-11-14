'use strict';
const fs = require('node:fs');
const {Changelog} = require('release-please/build/src/updaters/changelog');
const {Version} = require('release-please/build/src/version');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(new Changelog({version: Version.parse(input.version), changelogEntry: input.notes})
  .updateContent(input.content));
