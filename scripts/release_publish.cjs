// Keep native Release Please in charge; authenticate its actual mutation candidate.
'use strict';
const fs = require('node:fs');
const {GitHub, Manifest} = require('release-please');
const {DuplicateReleaseError} = require('release-please/build/src/errors');

async function tagCommit(github, tag) {
  const api = github.getGitHubApi().octokit;
  const repository = {owner: github.repository.owner, repo: github.repository.repo};
  let object;
  try {
    object = (await api.git.getRef({...repository, ref: `tags/${tag}`})).data.object;
  } catch (error) {
    if (error.status === 404) return null;
    throw error;
  }
  // GitHub's Git object schema fixes tag and commit as the types in a release tag chain.
  while (object.type === 'tag') {
    object = (await api.git.getTag({...repository, tag_sha: object.sha})).data.object;
  }
  if (object.type !== 'commit') throw Error('Release tag does not resolve to a commit');
  return object.sha;
}

async function publish(github, record) {
  const nativeCreate = github.createRelease.bind(github);
  github.createRelease = async (candidate, options) => {
    if (candidate.tag.toString() !== record.tag || candidate.tag.version.toString() !== record.version ||
        candidate.sha !== record.release) {
      throw Error('Native release candidate differs from the authenticated cut');
    }
    const existing = await tagCommit(github, record.tag);
    if (existing !== null && existing !== record.release) {
      throw Error('Existing immutable tag conflicts with the authenticated cut');
    }
    // Native creation may fill a missing tag, but this cut never requests force-tag behavior.
    return nativeCreate(candidate, {...options, forceTag: false});
  };
  const manifest = await Manifest.fromManifest(github, record.branch);
  let releases;
  try {
    // The guard sees each exact candidate inside this operation, after native parsing.
    releases = await manifest.createReleases();
  } catch (error) {
    if (!(error instanceof DuplicateReleaseError)) throw error;
    releases = [];
  }
  if (releases.length === 0) {
    if (await tagCommit(github, record.tag) !== record.release) {
      throw Error('Authenticated native release is still pending');
    }
    const existing = await github.getGitHubApi().octokit.repos.getReleaseByTag({
      owner: github.repository.owner, repo: github.repository.repo, tag: record.tag,
    });
    if (existing.data.tag_name !== record.tag) throw Error('Existing native release conflicts');
  }
  return releases;
}

module.exports = {publish};
if (require.main === module) {
  (async () => {
    const record = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
    const [owner, repo] = process.env.GH_REPO.split('/');
    const github = await GitHub.create({owner, repo, defaultBranch: 'main', token: process.env.GH_TOKEN,
      fetch: (url, options = {}) => fetch(url, {...options,
        signal: options.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(60000)]) : AbortSignal.timeout(60000)})});
    process.stdout.write(JSON.stringify(await publish(github, record)));
  })().catch(error => {console.error(error); process.exitCode = 1;});
}
