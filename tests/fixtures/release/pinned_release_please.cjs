// Recorded GitHub transport over real local objects. No network operation can escape.
'use strict';
const fs = require('node:fs');
const {execFileSync} = require('node:child_process');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const git = (...args) => execFileSync('git', args, {cwd: input.root, encoding: 'utf8'}).trimEnd();
const {GitHub, Manifest} = require('release-please');
require('release-please/build/src/util/logger').setLogger({info() {}, debug() {}, warn() {}, error() {}, trace() {}});
const pageInfo = {hasNextPage: false, endCursor: null};
const exchanges = [];
async function transport(url, options = {}) {
  const address = new URL(url);
  const method = options.method || 'GET';
  const body = options.body ? JSON.parse(options.body) : null;
  let data;
  let status = 200;
  if (address.pathname === '/graphql' && method === 'POST' && body.query.trim().startsWith('query')) {
    if (body.query.includes('releases(')) {
      data = {data: {repository: {releases: {pageInfo, nodes: input.releases.map(release => ({
        name: release.tag, tag: {name: release.tag}, tagCommit: {oid: release.sha},
        description: release.notes || '', url: 'https://example.invalid/release', isDraft: false,
      }))}}}};
    } else if (body.query.includes('history(')) {
      const nodes = git('log', '--format=%H', body.variables.targetBranch).split('\n').map(sha => ({
        sha, message: git('show', '-s', '--format=%B', sha),
        associatedPullRequests: {nodes: []}, author: null,
      }));
      data = {data: {repository: {ref: {target: {history: {pageInfo, nodes}}}}}};
    } else if (body.query.includes('pullRequests(')) {
      const nodes = (input.pullRequests || []).map(pr => ({
        number: pr.number, title: pr.title, body: pr.body, baseRefName: input.branch,
        headRefName: `release-please--branches--${input.branch}`, mergeCommit: {oid: pr.sha},
        labels: {nodes: [{name: 'autorelease: pending'}]}, files: {nodes: [], pageInfo},
      }));
      data = {data: {repository: {pullRequests: {pageInfo, nodes}}}};
    } else throw Error(`Unrecorded GraphQL query: ${body.query}`);
  } else if (method !== 'GET') {
    throw Error(`Outbound write blocked: ${method} ${url}`);
  } else {
    const prefix = '/repos/example/project/';
    if (!address.pathname.startsWith(prefix)) throw Error(`Unrecorded address: ${url}`);
    const path = decodeURIComponent(address.pathname.slice(prefix.length));
    if (path.startsWith('git/trees/')) {
      const ref = path.slice('git/trees/'.length);
      const rows = git('ls-tree', '-rz', '-r', ref).split('\0').filter(Boolean);
      data = {sha: git('rev-parse', `${ref}^{tree}`), truncated: false, tree: rows.map(row => {
        const [description, name] = row.split('\t');
        const [mode, type, sha] = description.split(' ');
        return {path: name, mode, type, sha};
      })};
    } else if (path.startsWith('git/blobs/')) {
      const sha = path.slice('git/blobs/'.length);
      data = {sha, encoding: 'base64', content: execFileSync('git', ['cat-file', 'blob', sha],
        {cwd: input.root}).toString('base64')};
    } else if (path.startsWith('commits/')) {
      const sha = path.slice('commits/'.length);
      data = {sha, files: git('diff-tree', '--root', '--no-commit-id', '--name-only', '-r', sha)
        .split('\n').filter(Boolean).map(filename => ({filename}))};
    } else if (path === 'pulls') {
      data = (input.pullRequests || []).map(pr => ({
        number: pr.number, title: pr.title, body: pr.body, merged_at: '2026-01-01T00:00:00Z',
        head: {ref: `release-please--branches--${input.branch}`}, base: {ref: input.branch},
        merge_commit_sha: pr.sha, labels: [{name: 'autorelease: pending'}],
      }));
    } else if (path.startsWith('releases/tags/')) {
      status = 404; data = {message: 'Not Found'};
    } else throw Error(`Unrecorded GET: ${path}`);
  }
  exchanges.push({method, path: address.pathname, request: body, status, response: data});
  return new Response(JSON.stringify(data), {status, headers: {'content-type': 'application/json'}});
}
globalThis.fetch = transport;
require('node:http').request = require('node:https').request = () => {throw Error('Network blocked');};
(async () => {
  const github = await GitHub.create({owner: 'example', repo: 'project', defaultBranch: 'main',
    token: 'fixture', fetch: transport});
  const manifest = await Manifest.fromManifest(github, input.branch);
  let output;
  if (input.operation === 'releases') {
    output = {releases: (await manifest.buildReleases()).map(candidate => ({
      tag: candidate.tag.toString(), sha: candidate.sha, notes: candidate.notes,
    }))};
  } else {
    const requests = await manifest.buildPullRequests();
    if (requests.length !== 1) throw Error(`Expected one native proposal, got ${requests.length}`);
    const pr = requests[0];
    const files = {};
    for (const update of pr.updates) {
      let before;
      try {before = git('show', `${input.branch}:${update.path}`) + '\n';}
      catch (error) {if (!update.createIfMissing) continue;}
      files[update.path] = update.updater.updateContent(before);
    }
    output = {title: pr.title.toString(), body: pr.body.toString(), files,
      version: JSON.parse(files['.release-please-manifest.json'])['.']};
  }
  fs.writeFileSync(input.recording, exchanges.map(row => JSON.stringify(row)).join('\n') + '\n');
  process.stdout.write(JSON.stringify(output));
})().catch(error => {console.error(error); process.exit(1);});
