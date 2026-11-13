// Use the same maintained updaters as the pinned Release Please action.
'use strict';
const fs = require('node:fs');
const {Generic} = require('release-please/build/src/updaters/generic');
const {GenericJson} = require('release-please/build/src/updaters/generic-json');
const {Version} = require('release-please/build/src/version');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const version = Version.parse(input.version);
const files = {...input.files};
const logger = {warn() {}, info() {}, debug() {}, error() {}};
for (const releasePackage of Object.values(input.config.packages)) {
  for (const configured of releasePackage['extra-files'] || []) {
    const entry = typeof configured === 'string' ? {path: configured, type: 'generic'} : configured;
    if (!(entry.path in files)) continue;
    let updater;
    if (entry.type === 'generic') updater = new Generic({version});
    else if (entry.type === 'json') updater = new GenericJson(entry.jsonpath, version);
    else throw Error(`Unsupported release updater: ${entry.type}`);
    files[entry.path] = updater.updateContent(files[entry.path], logger);
  }
}
if ('.release-please-manifest.json' in files) {
  const manifest = JSON.parse(files['.release-please-manifest.json']);
  for (const path of Object.keys(input.config.packages)) manifest[path] = version.toString();
  files['.release-please-manifest.json'] = JSON.stringify(manifest, null, 2) + '\n';
}
process.stdout.write(JSON.stringify(files));
