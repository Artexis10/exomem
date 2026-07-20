// Pure, DOM-free UI-model functions for the Adoption Studio.
// Node-testable exactly like model.v1.js. No URLs, no vault strings, no storage.
//
// Selection model: folders default ON. `sel` records only EXPLICIT choices:
//   sel.folders[path] = true|false   (a folder rule the user set)
//   sel.files[path]   = true|false   (a per-file override against its folder)
//   sel.includeJunk   = bool
// The engine materializes the concrete file set from {include, exclude,
// overrides, include_junk} against the depth-capped tree (design.md Decision 3).

export function initialSelection(_tree, _junk) {
  return {folders: {}, files: {}, includeJunk: false};
}

function underFolder(path, folder) {
  return path === folder || path.startsWith(`${folder}/`);
}

export function toggleFolder(sel, path, on) {
  const folders = {...sel.folders, [path]: !!on};
  // A folder rule is authoritative for its subtree: clear descendant rules/overrides.
  for (const key of Object.keys(folders)) {
    if (key !== path && underFolder(key, path)) delete folders[key];
  }
  const files = {...sel.files};
  for (const key of Object.keys(files)) {
    if (underFolder(key, path)) delete files[key];
  }
  return {...sel, folders, files};
}

export function overrideFile(sel, path, on) {
  return {...sel, files: {...sel.files, [path]: !!on}};
}

// Effective ON/OFF for a single file path given the folder rules + overrides.
function effectiveFileOn(sel, path) {
  if (Object.prototype.hasOwnProperty.call(sel.files, path)) return !!sel.files[path];
  let best = null;
  for (const folder of Object.keys(sel.folders)) {
    if (underFolder(path, folder) && (best === null || folder.length > best.length)) best = folder;
  }
  if (best !== null) return !!sel.folders[best];
  return true;
}

export function folderState(sel, tree, path) {
  const on = Object.prototype.hasOwnProperty.call(sel.folders, path) ? !!sel.folders[path] : true;
  for (const node of tree || []) {
    const child = node && node.path;
    if (!child || child === path || !underFolder(child, path)) continue;
    if (Object.prototype.hasOwnProperty.call(sel.folders, child) && !!sel.folders[child] !== on) {
      return "mixed";
    }
  }
  for (const file of Object.keys(sel.files)) {
    if (underFolder(file, path) && !!sel.files[file] !== on) return "mixed";
  }
  return on ? "checked" : "unchecked";
}

export function selectionCounts(inventory, sel) {
  const rows = Array.isArray(inventory)
    ? inventory
    : (inventory && (inventory.rows || inventory.inventory)) || [];
  let selectableNotes = 0;
  let selectedNotes = 0;
  let junkFromRows = 0;
  for (const row of rows) {
    if (row && row.junk) junkFromRows += 1;
    if (!row || !row.eligible) continue;
    selectableNotes += 1;
    if (effectiveFileOn(sel, row.path)) selectedNotes += 1;
  }
  const junkAvailable = !Array.isArray(inventory) && inventory && typeof inventory.junk_count === "number"
    ? inventory.junk_count
    : junkFromRows;
  return {
    selectedNotes,
    selectableNotes,
    junkAvailable,
    junkIncluded: sel.includeJunk ? junkAvailable : 0,
  };
}

export function selectionPayload(sel, roots = []) {
  const include = [];
  const exclude = [];
  for (const [key, value] of Object.entries(sel.folders)) (value ? include : exclude).push(key);
  // Folders default ON, but the engine materializes additively from explicit
  // rules — so every untouched top-level root becomes an explicit include,
  // and deeper OFF rules win by the engine's specificity ordering.
  for (const root of roots) {
    if (!Object.prototype.hasOwnProperty.call(sel.folders, root)) include.push(root);
  }
  // Engine overrides are add-only: an OFF file override is a file-path exclude.
  const overrides = [];
  for (const [key, value] of Object.entries(sel.files)) (value ? overrides : exclude).push(key);
  return {
    include: include.sort(),
    exclude: exclude.sort(),
    overrides: overrides.sort(),
    include_junk: !!sel.includeJunk,
  };
}

// Inverse of selectionPayload: rebuild the explicit-choice model from a run's
// persisted selection rules so a URL resume never silently resets the user's
// exclusions and overrides. File-vs-folder is decided by inventory membership.
export function selectionFromRules(rules, inventoryPaths) {
  if (!rules) return null;
  const files = new Set(inventoryPaths || []);
  const sel = {folders: {}, files: {}, includeJunk: !!rules.include_junk};
  for (const p of rules.include || []) {
    if (files.has(p)) sel.files[p] = true;
    else sel.folders[p] = true;
  }
  for (const p of rules.exclude || []) {
    if (files.has(p)) sel.files[p] = false;
    else sel.folders[p] = false;
  }
  for (const p of rules.overrides || []) sel.files[p] = true;
  return sel;
}

export function planBullets(totals) {
  const copy = Number((totals && totals.copy) || 0);
  const unsupported = Number((totals && totals.skip_unsupported) || 0);
  const junk = Number((totals && totals.skip_junk) || 0);
  const total = copy + unsupported + junk;
  const bullets = [
    `${copy} text notes will be copied in`,
    `${unsupported} photos & other files stay put (not copied — not supported yet)`,
    `${junk} junk files will be skipped`,
    "0 files will be changed, moved, or deleted — always",
  ];
  return {bullets, total, copy, unsupported, junk};
}

export function countLine(shown, total, omitted = 0) {
  const parts = [`Showing ${shown} of ${total}`];
  if (Number(omitted) > 0) parts.push(`${omitted} not shown`);
  return parts.join(" · ");
}

const POST_APPLY_STEPS = ["organize", "suggestions", "question"];
const LEGAL_STEPS = {
  selecting: {allowed: ["findings", "choose", "preview"], def: "findings"},
  planned: {allowed: ["findings", "choose", "preview"], def: "preview"},
  applied: {allowed: POST_APPLY_STEPS, def: "start"},
  partial: {allowed: POST_APPLY_STEPS, def: "start"},
  done: {allowed: POST_APPLY_STEPS, def: "start"},
};

export function legalStep(phase, astep) {
  const spec = LEGAL_STEPS[phase];
  if (!spec) return "start"; // applying / failed / cancelled / unknown → phase-driven
  return spec.allowed.includes(astep) ? astep : spec.def;
}

export function phaseScreen(run, astep) {
  const phase = run && run.phase;
  if (!run || !phase) return "start";
  switch (phase) {
    case "selecting":
      if (astep === "choose") return "choose";
      if (astep === "preview") return "preview";
      return "findings";
    case "planned":
      if (astep === "choose") return "choose";
      if (astep === "findings") return "findings";
      return "preview";
    case "applying":
      return "applying";
    case "applied":
    case "partial":
      if (astep === "organize") return "handoff";
      if (astep === "suggestions") return "proposals";
      if (astep === "question") return "question";
      return "result";
    case "done":
      if (astep === "organize") return "handoff";
      if (astep === "suggestions") return "proposals";
      if (astep === "question") return "question";
      return "done";
    case "cancelled":
      return "cancelled";
    case "failed":
      return "failed";
    default:
      return "unknown";
  }
}

const FAILURE_REASONS = {
  UNSUPPORTED_IMPORT_TYPE: "This kind of file isn't supported yet.",
  ALREADY_GOVERNED: "Already in Exomem's library — no copy needed.",
  NOT_FOUND: "This file moved or was removed after the scan.",
  SOURCE_CHANGED: "This file changed after we looked, so we left it untouched.",
  BATCH_ROLLED_BACK: "Nothing was written for this file — a safety check undid the batch.",
};

export function failureGroups(failed) {
  const groups = new Map();
  for (const item of failed || []) {
    const code = (item && item.code) || "UNKNOWN";
    const plain = FAILURE_REASONS[code]
      || (item && item.reason ? `Couldn't be copied: ${item.reason}` : "Couldn't be copied.");
    if (!groups.has(code)) groups.set(code, {code, reason: plain, paths: []});
    groups.get(code).paths.push(item && item.path);
  }
  return [...groups.values()];
}

export function staleNotice(detail) {
  const raw = detail && (detail.changed_count ?? detail.changedCount);
  const count = Number(raw) || 0;
  const files = count === 1 ? "1 file" : `${count} files`;
  return `Your folder changed since we looked (${files} changed or moved). `
    + "Let's re-check so this plan stays accurate. Nothing has been copied yet.";
}

// Polling cadence (setTimeout chain). Kinds: "scanning"/"applying" (1.5s → 4s
// after 60s) and "awaiting_proposals" (5s → 15s after the 120s timeout notice).
export function pollDelay(kind, elapsedMs) {
  const elapsed = Number(elapsedMs) || 0;
  if (kind === "awaiting_proposals") return elapsed >= 120000 ? 15000 : 5000;
  if (kind === "scanning" || kind === "applying") return elapsed >= 60000 ? 4000 : 1500;
  return 4000;
}

export function suggestionChips(packSuggestions, tree) {
  const chips = [];
  const seen = new Set();
  const add = (name) => {
    const clean = String(name || "").trim();
    if (!clean) return;
    const key = clean.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    chips.push({label: `Find my notes on ${clean}`, query: clean});
  };
  for (const pack of packSuggestions || []) add(pack && pack.name);
  if (chips.length < 4) {
    for (const node of tree || []) {
      const base = String((node && node.path) || "").split("/").pop();
      add(base);
      if (chips.length >= 4) break;
    }
  }
  return chips.slice(0, 4);
}

// Fixed, generic consequence stated before a reviewed-none approval: approving a
// proposal whose content carries no typed relation records it as reviewed-with-
// none, and the page resurfaces later for relation review (engine reviewed-none
// flow). No vault strings, no URLs — honesty about what approval will record.
const REVIEWED_NONE_CONSEQUENCE =
  "Approving records this as reviewed with no typed relation yet — "
  + "it will come back for relation review.";

// Render-only model for a proposal's semantic-write-contract findings. Turns the
// engine's compact `contract_findings` (code/severity/detail) into display lines,
// states the reviewed-none consequence when the proposal needs a relation review,
// and disables approval when the server already marked the proposal `invalid`
// (this is honesty in the UI — the server refuses invalid applies regardless).
export function contractFindingsView(context) {
  const source = (context && context.contract_findings) || [];
  const findings = [];
  for (const f of source) {
    if (!f) continue;
    const detail = String(f.detail || "").trim();
    findings.push({
      code: f.code || "",
      severity: f.severity || "",
      text: detail || String(f.code || "This change was flagged by the write contract."),
    });
  }
  return {
    findings,
    hasFindings: findings.length > 0,
    consequence: context && context.reviewed_none_required ? REVIEWED_NONE_CONSEQUENCE : "",
    approveDisabled: !!(context && context.status === "invalid"),
  };
}

// Total junk file count for a scan. A present `junk_counts` map is authoritative
// even when it sums to zero; only an ABSENT map falls back to counting the junk-
// flagged inventory rows. (The old `sum || fallback` mis-counted a present-but-
// zero map by treating the falsy 0 as "no data".)
export function junkCount(scanSummary, junkRowCount = 0) {
  const counts = scanSummary && scanSummary.junk_counts;
  if (counts && typeof counts === "object") {
    return Object.values(counts).reduce((sum, value) => sum + Number(value || 0), 0);
  }
  return Number(junkRowCount) || 0;
}
