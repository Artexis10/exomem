// Adoption Studio controller. Owns the nine-screen guided journey, polling,
// dialog confirms (reusing #action-dialog), stale/error mapping, and the single
// `adoptionApi` adapter — the ONLY place backend command names appear, so
// reconciling with the landed engine is a one-line-per-entry change.
//
// Contract source of truth: design.md Decision 1 (one command `adoption_studio`
// with ten actions) + Decision 3 (phases / error vocabulary). UI verb → engine
// action: preview→plan, approve→apply-proposal, create→start, retry→
// apply(retry_failed=true, only_paths). Assets stay inert: no URLs, no vault
// strings, no storage — the handoff prompt_text and links come from the API.

import {ApiError, command} from "/studio/assets/api.v1.js";
import {
  countLine,
  failureGroups,
  folderState,
  initialSelection,
  legalStep,
  overrideFile,
  phaseScreen,
  planBullets,
  pollDelay,
  selectionCounts,
  selectionFromRules,
  selectionPayload,
  staleNotice,
  suggestionChips,
  toggleFolder,
} from "/studio/assets/adoption-model.v1.js";
import {readRoute, routePatch, writeRoute} from "/studio/assets/state.v2.js";

// --- The single backend adapter (design.md Decision 1 action names) --------- //

async function adoptionMutation(body) {
  const result = await command("adoption_studio", {
    ...body,
    response_detail: "full",
  });
  if (!result || typeof result.diagnostics !== "object") {
    throw new ApiError("The adoption mutation returned no terminal diagnostics.");
  }
  return result.diagnostics;
}

const adoptionApi = {
  start: (path, {initializeKb = false} = {}) =>
    adoptionMutation({action: "start", path, initialize_kb: initializeKb}),
  // No dedicated discovery action in the engine contract; resume rides a run id
  // already present in the URL. Returns null until such an action exists.
  latest: async () => null,
  status: (runId) => command("adoption_studio", {action: "status", run_id: runId}),
  select: (runId, payload) =>
    adoptionMutation({action: "select", run_id: runId, ...payload}),
  plan: (runId) => adoptionMutation({action: "plan", run_id: runId}),
  apply: (runId, planId) =>
    adoptionMutation({action: "apply", run_id: runId, plan_id: planId}),
  // apply always echoes plan_id (even on retry): the engine refuses a
  // mismatched/missing plan_id with PLAN_STALE regardless of retry_failed.
  retry: (runId, planId, onlyPaths) =>
    adoptionMutation({
      action: "apply",
      run_id: runId,
      plan_id: planId,
      retry_failed: true,
      only_paths: onlyPaths && onlyPaths.length ? onlyPaths : null,
    }),
  cancel: (runId, why) =>
    adoptionMutation({action: "cancel", run_id: runId, why: why || null}),
  finish: (runId) => adoptionMutation({action: "finish", run_id: runId}),
  workItem: (runId) => command("adoption_studio", {action: "work-item", run_id: runId}),
  // Scoped by the run's ref so a run's review screen never shows (or acts on)
  // another run's proposals.
  proposals: (runRef) =>
    command("review_memory", {mode: "adoption", ref: runRef || null, limit: 50}),
  proposalContext: (ref, fingerprint) =>
    command("review_item_context", {ref, expected_fingerprint: fingerprint}),
  approveProposal: (ref, fingerprint, why, expectedHash) =>
    command("adoption_studio", {
      action: "apply-proposal",
      ref,
      expected_fingerprint: fingerprint,
      why,
      // Relation-kind approvals are CAS-guarded on the target page: echo the
      // content_hash the reviewer just inspected.
      expected_hash: expectedHash || null,
    }),
  rejectProposal: (ref, fingerprint, why) =>
    command("triage_memory", {
      ref,
      action: "dismiss",
      why: why || null,
      expected_fingerprint: fingerprint,
    }),
  ask: (query) => command("ask_memory", {query, limit: 5, detail: "compact"}),
};

// --- DOM helpers ------------------------------------------------------------ //

const byId = (id) => document.getElementById(id);
const dialog = () => byId("action-dialog");

function el(tag, text = "", className = "") {
  const node = document.createElement(tag);
  if (text !== "") node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function replaceChildren(target, children = []) {
  if (target) target.replaceChildren(...children.filter(Boolean));
}

const SCREENS = [
  "start", "scanning", "findings", "choose", "preview", "applying", "result",
  "handoff", "proposals", "question", "done", "cancelled", "failed", "unknown",
];

function showScreen(name) {
  for (const key of SCREENS) {
    const section = byId(`adopt-${key}`);
    if (section) section.hidden = key !== name;
  }
  const step = STEP_FOR_SCREEN[name];
  for (const item of document.querySelectorAll("#adopt-stepper [data-step]")) {
    item.setAttribute("aria-current", item.dataset.step === String(step) ? "step" : "false");
  }
  const compact = byId("adopt-stepper-compact");
  if (compact) compact.textContent = step ? `Step ${step} of 7 · ${STEP_LABELS[step]}` : "";
}

const STEP_FOR_SCREEN = {
  start: 1, scanning: 1, findings: 2, choose: 3, preview: 4,
  applying: 5, result: 5, handoff: 6, proposals: 6, question: 7, done: 7,
};
const STEP_LABELS = {
  1: "Look around", 2: "What we found", 3: "Choose", 4: "Check the plan",
  5: "Bring in", 6: "Organize", 7: "First question",
};

function setStatus(text) {
  const node = byId("adopt-status");
  if (node) node.textContent = text || "";
}

function showAdoptError(error) {
  const node = byId("adopt-error");
  if (!node) return;
  const message = error instanceof ApiError
    ? (error.remediation ? `${error.message} ${error.remediation}` : error.message)
    : "Something went wrong. Your originals are untouched.";
  node.textContent = message;
  node.hidden = false;
  node.focus?.();
}

function clearAdoptError() {
  const node = byId("adopt-error");
  if (node) {
    node.hidden = true;
    node.textContent = "";
  }
}

function showDialogError(error) {
  const node = byId("dialog-error");
  if (!node) return;
  node.textContent = error instanceof ApiError ? error.message : String(error || "Action failed.");
  node.hidden = false;
}

// A confirm modal reusing #action-dialog. The confirm button is type=submit; a
// preventDefault on its click stops the review form-submit handler from firing.
function openConfirm({kicker, title, description, confirmLabel, cancelLabel = "Cancel", onConfirm}) {
  const dlg = dialog();
  byId("dialog-kicker").textContent = kicker;
  byId("dialog-title").textContent = title;
  byId("dialog-description").textContent = description;
  replaceChildren(byId("dialog-fields"));
  byId("dialog-error").hidden = true;
  const confirm = byId("dialog-confirm");
  const cancel = byId("dialog-cancel");
  confirm.textContent = confirmLabel;
  cancel.textContent = cancelLabel;
  confirm.disabled = false;
  const onConfirmClick = async (event) => {
    event.preventDefault();
    confirm.disabled = true;
    try {
      await onConfirm();
      dlg.close();
    } catch (error) {
      showDialogError(error);
      confirm.disabled = false;
    }
  };
  const cleanup = () => {
    confirm.removeEventListener("click", onConfirmClick);
    dlg.removeEventListener("close", cleanup);
    cancel.textContent = "Cancel";
  };
  confirm.addEventListener("click", onConfirmClick);
  dlg.addEventListener("close", cleanup);
  dlg.showModal();
}

// --- Module state ----------------------------------------------------------- //

let route = null;
let run = null;
let selection = null;
let filesOffset = 0;
let openFolder = "";
let pollTimer = null;
let pollStart = 0;
let wired = false;

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

// --- Inventory / tree derivation (works off the guaranteed flat inventory) --- //

function inventoryRows() {
  return (run && Array.isArray(run.inventory)) ? run.inventory : [];
}

function inventoryTotals() {
  const totals = (run && run.scan_summary && run.scan_summary.totals) || {};
  return {
    files: Number(totals.files || 0),
    dirs: Number(totals.dirs || 0),
    markdown: Number(totals.markdown || 0),
    binary: Number(totals.binary || 0),
  };
}

function junkTotal() {
  const counts = (run && run.scan_summary && run.scan_summary.junk_counts) || {};
  return Object.values(counts).reduce((sum, value) => sum + Number(value || 0), 0)
    || inventoryRows().filter((row) => row && row.junk).length;
}

// Derive a depth-capped folder node list from inventory paths for tri-state.
function derivedTree() {
  const folders = new Map();
  for (const row of inventoryRows()) {
    const path = String((row && row.path) || "");
    const parts = path.split("/");
    for (let depth = 1; depth < parts.length && depth <= 3; depth += 1) {
      const folder = parts.slice(0, depth).join("/");
      if (!folders.has(folder)) {
        folders.set(folder, {path: folder, notes: 0, files: 0, depth});
      }
    }
    const parent = parts.slice(0, -1).join("/");
    if (parent && folders.has(parent)) {
      folders.get(parent).files += 1;
      if (row && row.eligible) folders.get(parent).notes += 1;
    }
  }
  return [...folders.values()];
}

function topFolders() {
  return derivedTree().filter((node) => node.depth === 1);
}

// --- Render dispatch -------------------------------------------------------- //

function render() {
  clearAdoptError();
  const screen = run ? phaseScreen(run, route.astep) : phaseScreen(null, route.astep);
  // Snap the URL to the legal step for the server's phase (server phase wins).
  if (run && run.phase) {
    const legal = legalStep(run.phase, route.astep);
    if (legal !== route.astep) {
      route = routePatch(route, {astep: legal});
      writeRoute(route, {replace: true});
    }
  }
  showScreen(screen);
  const populate = POPULATE[screen];
  if (populate) populate();
  managePolling(screen);
}

function goStep(astep) {
  route = routePatch(route, {astep});
  writeRoute(route);
  render();
}

const POPULATE = {
  start: populateStart,
  scanning: () => setStatus("This usually takes under a minute."),
  findings: populateFindings,
  choose: populateChoose,
  preview: populatePreview,
  applying: populateApplying,
  result: populateResult,
  handoff: populateHandoff,
  proposals: populateProposals,
  question: populateQuestion,
  done: () => {},
  cancelled: populateCancelled,
  failed: populateFailed,
  unknown: () => {},
};

// --- Screen 1: Start -------------------------------------------------------- //

function populateStart() {
  setStatus("");
  const resume = byId("adopt-resume");
  if (resume) resume.hidden = true;
}

async function doStart() {
  clearAdoptError();
  const path = byId("adopt-path").value.trim();
  showScreen("scanning");
  setStatus("Looking through your files…");
  try {
    run = await adoptionApi.start(path, {});
    route = routePatch(route, {run: run.run_id || run.run || "", astep: "findings"});
    writeRoute(route, {replace: true});
    // start returns synchronously with inventory (phase `selecting`).
    if (isTransientPhase(run.phase)) {
      await refreshStatus();
    }
    render();
  } catch (error) {
    if (error instanceof ApiError && error.code === "KB_NOT_INITIALIZED") {
      offerKbSetup(path);
      return;
    }
    showScreen("start");
    showAdoptError(error);
  }
}

function offerKbSetup(path) {
  showScreen("start");
  const node = byId("adopt-error");
  node.hidden = false;
  node.textContent = "";
  node.append(
    el("span", "One quick step first: Exomem needs its own library folder next to your files. "),
  );
  const button = el("button", "Set it up");
  button.type = "button";
  button.addEventListener("click", async () => {
    clearAdoptError();
    showScreen("scanning");
    setStatus("Setting up your library…");
    try {
      run = await adoptionApi.start(path, {initializeKb: true});
      route = routePatch(route, {run: run.run_id || "", astep: "findings"});
      writeRoute(route, {replace: true});
      render();
    } catch (error) {
      showScreen("start");
      showAdoptError(error);
    }
  });
  node.append(button);
  node.append(el("span", " This creates one new folder and touches nothing else."));
  node.focus?.();
}

// --- Screen 3: Findings ----------------------------------------------------- //

function populateFindings() {
  setStatus("");
  const totals = inventoryTotals();
  const junk = junkTotal();
  const tiles = [
    tile(totals.files, "files"),
    tile(totals.dirs, "folders"),
    tile(totals.markdown, "notes & text"),
    tile(totals.binary, "photos & other"),
    tile(junk, "look like junk"),
  ];
  replaceChildren(byId("adopt-tiles"), tiles);

  const packs = (run && run.pack_suggestions) || [];
  const packLine = byId("adopt-packs");
  if (packLine) {
    packLine.textContent = packs.length
      ? `Your folder looks like it includes: ${packs.map((p) => p.name).join(" · ")}`
      : "";
    packLine.hidden = !packs.length;
  }

  const folders = topFolders();
  replaceChildren(byId("adopt-folder-list"), folders.map((folder) => {
    const li = el("li");
    li.append(el("strong", folder.path));
    li.append(el("span", ` ${folder.files} files · ${folder.notes} notes`, "fine-print"));
    return li;
  }));

  const junkNode = byId("adopt-junk");
  if (junkNode) {
    junkNode.textContent = junk
      ? `Probably junk — ${junk} files (empty files, sync-conflict copies). We'll skip these unless you say otherwise.`
      : "";
    junkNode.hidden = !junk;
  }

  const nonText = byId("adopt-nontext");
  if (nonText) {
    nonText.textContent = totals.binary
      ? `Photos, PDFs and other non-text files: ${totals.binary} — Exomem can see them, but this brings in text notes only. They stay put; you can add them later.`
      : "";
    nonText.hidden = !totals.binary;
  }

  if (!totals.files) {
    setStatus("This folder looks empty (0 files). Pick a different folder, or check the path.");
  }
}

function tile(value, label) {
  const node = el("div", "", "tile");
  node.append(el("strong", String(value)));
  node.append(el("span", label));
  return node;
}

// --- Screen 4: Choose ------------------------------------------------------- //

function ensureSelection() {
  if (!selection) selection = initialSelection(derivedTree(), run && run.scan_summary);
}

function populateChoose() {
  ensureSelection();
  renderChooseStatus();
  const tree = topFolders();
  const rows = tree.map((folder) => renderFolderRow(folder));
  replaceChildren(byId("adopt-choose-tree"), rows);
  const junkToggle = byId("adopt-junk-toggle");
  if (junkToggle) {
    junkToggle.checked = !!selection.includeJunk;
    const junk = junkTotal();
    const label = byId("adopt-junk-label");
    if (label) label.textContent = `Include the ${junk} probably-junk files too`;
  }
  const panel = byId("adopt-files-panel");
  if (panel) panel.hidden = true;
}

function renderChooseStatus() {
  const counts = selectionCounts(inventoryRows(), selection);
  setStatus(
    `${counts.selectedNotes} of ${counts.selectableNotes} text notes selected · `
    + (selection.includeJunk ? `${counts.junkIncluded} junk included` : "junk skipped"),
  );
}

function renderFolderRow(folder) {
  const tree = derivedTree();
  const li = el("li", "", "tri-row");
  li.setAttribute("role", "treeitem");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.id = `adopt-folder-${cssId(folder.path)}`;
  const state = folderState(selection, tree, folder.path);
  checkbox.checked = state === "checked";
  checkbox.indeterminate = state === "mixed";
  checkbox.addEventListener("change", () => {
    selection = toggleFolder(selection, folder.path, checkbox.checked);
    populateChoose();
  });
  const label = el("label", "");
  label.htmlFor = checkbox.id;
  label.append(el("strong", folder.path));
  label.append(el("span", ` ${folder.notes} notes`, "fine-print"));
  li.append(checkbox, label);
  const seeFiles = el("button", "see files", "link-button");
  seeFiles.type = "button";
  seeFiles.setAttribute("aria-expanded", String(openFolder === folder.path));
  seeFiles.addEventListener("click", () => {
    openFolder = openFolder === folder.path ? "" : folder.path;
    filesOffset = 0;
    renderFilesPanel();
  });
  li.append(seeFiles);
  return li;
}

function renderFilesPanel() {
  const panel = byId("adopt-files-panel");
  if (!panel) return;
  if (!openFolder) {
    panel.hidden = true;
    return;
  }
  const all = inventoryRows().filter((row) => {
    const path = String((row && row.path) || "");
    return path === openFolder || path.startsWith(`${openFolder}/`);
  });
  const shown = all.slice(0, filesOffset + 200);
  const list = el("ul", "", "file-table");
  list.setAttribute("role", "group");
  shown.forEach((row, index) => {
    const li = el("li", "", "file-row");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = effectiveOn(row.path);
    checkbox.disabled = !row.eligible;
    checkbox.tabIndex = index === 0 ? 0 : -1;
    checkbox.addEventListener("change", () => {
      selection = overrideFile(selection, row.path, checkbox.checked);
      renderChooseStatus();
      populateChoose();
    });
    const label = el("label", row.eligible ? row.path : `${row.path} — can't be copied yet (not a text file)`);
    li.append(checkbox, label);
    return list.append(li);
  });
  const heading = el("p", countLine(shown.length, all.length, Math.max(0, all.length - shown.length)) + " · use folder checkboxes to narrow down", "fine-print");
  const children = [heading, list];
  if (shown.length < all.length) {
    const more = el("button", "Show 200 more", "secondary");
    more.type = "button";
    more.addEventListener("click", () => {
      filesOffset += 200;
      renderFilesPanel();
    });
    children.push(more);
  }
  replaceChildren(panel, children);
  panel.hidden = false;
  wireRovingTabindex(list);
}

function effectiveOn(path) {
  const counts = selectionCounts([{path, eligible: true}], selection);
  return counts.selectedNotes === 1;
}

function selectionRoots() {
  const roots = new Set();
  for (const row of inventoryRows()) {
    if (!row || !row.eligible) continue;
    const path = String(row.path || "");
    const cut = path.indexOf("/");
    roots.add(cut === -1 ? path : path.slice(0, cut));
  }
  return [...roots].sort();
}

async function persistSelection() {
  if (!run) return;
  const payload = selectionPayload(selection, selectionRoots());
  const result = await adoptionApi.select(run.run_id, payload);
  if (result && typeof result.selected_count === "number") {
    setStatus(`${result.selected_count} of ${result.selectable_count} text notes selected · junk ${selection.includeJunk ? "included" : "skipped"}`);
  }
  return result;
}

// --- Screen 5: Preview ------------------------------------------------------ //

async function enterPreview() {
  clearAdoptError();
  showScreen("preview");
  setStatus("Working out exactly what will happen…");
  try {
    await persistSelection();
    // Every adoption_studio action returns the FULL presented run document
    // (uniform shape) — replace `run` wholesale rather than nesting the
    // response under a synthetic key.
    run = await adoptionApi.plan(run.run_id);
    populatePreview();
  } catch (error) {
    if (isStale(error)) {
      showStaleBanner(error);
      return;
    }
    if (error instanceof ApiError && error.code === "MISSING_SELECTION") {
      goStep("choose");
      showAdoptError(new ApiError("Choose at least one folder or file to bring in."));
      return;
    }
    showAdoptError(error);
  }
}

function populatePreview() {
  setStatus("");
  const banner = byId("adopt-stale");
  if (banner) banner.hidden = true;
  const plan = (run && run.plan) || {};
  const totals = plan.totals || derivedPlanTotals(plan);
  const contract = byId("adopt-contract");
  if (contract) {
    contract.textContent = `Exomem will COPY ${totals.copy || 0} files into its own library. `
      + "Copies, never moves: every original stays exactly where it is.";
  }
  const {bullets} = planBullets(totals);
  replaceChildren(byId("adopt-plan-bullets"), bullets.map((text) => el("li", text)));

  const items = plan.items || [];
  const details = byId("adopt-plan-files");
  if (details) {
    const summary = el("summary", `See every file (${Math.min(200, items.length)} shown of ${items.length})`);
    const list = el("ul", "", "file-table");
    items.slice(0, 200).forEach((item) => {
      const li = el("li");
      li.append(el("span", `${item.original_path}  →  copied in as "${item.title || item.target_name || ""}"`));
      li.append(el("span", "We record where it came from and a checksum, so you can verify your original any time.", "fine-print"));
      list.append(li);
    });
    replaceChildren(details, [summary, list]);
  }
}

function derivedPlanTotals(plan) {
  const skipped = plan.skipped || [];
  return {
    copy: (plan.items || []).length,
    skip_unsupported: skipped.filter((s) => s.code === "UNSUPPORTED_IMPORT_TYPE").length,
    skip_junk: skipped.filter((s) => s.code !== "UNSUPPORTED_IMPORT_TYPE").length,
  };
}

function confirmApply() {
  const plan = (run && run.plan) || {};
  const totals = plan.totals || derivedPlanTotals(plan);
  openConfirm({
    kicker: "YOU STAY IN CONTROL",
    title: `Copy ${totals.copy || 0} files into Exomem?`,
    description: "This adds copies to Exomem's library. Your originals are not touched.",
    confirmLabel: "Yes, copy them in",
    cancelLabel: "Not yet",
    onConfirm: async () => {
      const planId = plan.plan_id;
      run = await adoptionApi.apply(run.run_id, planId);
      route = routePatch(route, {astep: "start"});
      writeRoute(route, {replace: true});
      if (isTransientPhase(run.phase)) await refreshStatus();
      render();
    },
  });
}

function showStaleBanner(error) {
  showScreen("preview");
  const banner = byId("adopt-stale");
  if (!banner) return;
  // The REST 409 error envelope carries only {code, message, remediation} — no
  // structured changed-file count — so the honest banner text is the server's
  // own reason, never a fabricated number.
  const text = byId("adopt-stale-text");
  if (text) {
    text.textContent = error instanceof ApiError
      ? `Your folder changed since we looked: ${error.message} Nothing has been copied yet.`
      : staleNotice({});
  }
  banner.hidden = false;
}

async function recheckFolder() {
  clearAdoptError();
  showScreen("scanning");
  setStatus("Re-checking your folder…");
  try {
    run = await adoptionApi.status(run.run_id);
    goStep("choose");
  } catch (error) {
    showAdoptError(error);
  }
}

// --- Screen 5b: Applying ---------------------------------------------------- //

function populateApplying() {
  const progress = (run && run.progress) || {};
  if (typeof progress.done === "number") {
    setStatus(`Copied ${progress.done} of ${progress.total} .`);
  } else {
    setStatus("Bringing your files in…");
  }
}

function confirmStopApply() {
  openConfirm({
    kicker: "YOU STAY IN CONTROL",
    title: "Stop bringing files in?",
    description: "Files already copied stay in Exomem; nothing more will be added. Your originals are untouched either way.",
    confirmLabel: "Stop now",
    cancelLabel: "Keep going",
    onConfirm: async () => {
      run = await adoptionApi.cancel(run.run_id, "user stopped apply");
      render();
    },
  });
}

// --- Screen 6: Result ------------------------------------------------------- //

function populateResult() {
  setStatus("");
  const result = applyResult();
  const failed = result.failed || [];
  const copied = result.copied || [];
  const title = byId("adopt-result-title");
  if (title) {
    title.textContent = failed.length
      ? `${copied.length} files are in · ${failed.length} couldn't be copied`
      : `All set — ${copied.length} files are in ✓`;
  }
  const verify = byId("adopt-verify");
  if (verify) verify.textContent = verificationLine();

  const groups = failureGroups(failed);
  const failuresNode = byId("adopt-failures");
  if (failuresNode) {
    replaceChildren(failuresNode, groups.map((group) => {
      const block = el("div", "", "failure-group");
      block.append(el("p", group.reason, "reason"));
      const list = el("ul", "", "record-list");
      for (const path of group.paths) list.append(el("li", path));
      block.append(list);
      return block;
    }));
    failuresNode.hidden = !groups.length;
  }
  const retry = byId("adopt-retry");
  if (retry) {
    retry.hidden = !failed.length;
    retry.textContent = `Try those ${failed.length} again`;
    retry.dataset.paths = JSON.stringify(failed.map((item) => item.path));
  }
}

// There is no `apply_result.copied`/`.failed` on the real run document — the
// per-item detail lives ONLY in the `outcomes` map (keyed by original path).
// `apply_result` (and top-level `verified_unchanged`/`verified_total`) carries
// just the re-hash counts; always derive copied/failed from `outcomes`.
function applyResult() {
  return outcomesToResult();
}

function outcomesToResult() {
  const outcomes = (run && run.outcomes) || {};
  const copied = [];
  const failed = [];
  for (const [path, outcome] of Object.entries(outcomes)) {
    if (outcome.status === "applied" || outcome.status === "already-applied") copied.push({original_path: path, ...outcome});
    else if (outcome.status === "failed") failed.push({path, code: outcome.code, reason: outcome.reason});
  }
  return {copied, failed};
}

function verificationLine() {
  // verified_unchanged/verified_total appear TOP-LEVEL on the run document
  // once it has applied outcomes (the real post-apply re-hash counts) — never
  // nested under apply_result/result/finish.
  const unchanged = run && run.verified_unchanged;
  const total = run && run.verified_total;
  if (typeof unchanged === "number" && typeof total === "number") {
    return `We double-checked your originals: ${unchanged} of ${total} are byte-for-byte unchanged (checksums match).`;
  }
  return "We didn't re-check your originals this time — but nothing was moved, edited, or deleted.";
}

async function doRetry(paths) {
  clearAdoptError();
  showScreen("applying");
  setStatus("Bringing the rest of your files in…");
  try {
    run = await adoptionApi.retry(run.run_id, run.plan && run.plan.plan_id, paths);
    if (isTransientPhase(run.phase)) await refreshStatus();
    render();
  } catch (error) {
    showAdoptError(error);
    render();
  }
}

// --- Screen 7: Organize handoff --------------------------------------------- //

async function populateHandoff() {
  setStatus("");
  const wait = byId("adopt-handoff-wait");
  if (wait) wait.hidden = true;
  let handoff = (run && run.handoff) || null;
  if (!handoff) {
    try {
      // `status` is read-only and always surfaces `handoff` (per design, not
      // only after `finish`) — prefer it so merely viewing this optional,
      // skippable step never forces the run to phase "done".
      run = await adoptionApi.status(run.run_id);
      handoff = run.handoff || null;
    } catch (error) {
      showAdoptError(error);
      return;
    }
  }
  const textarea = byId("adopt-prompt");
  if (textarea) textarea.value = (handoff && handoff.prompt_text) || "";
  renderHandoffLinks((handoff && handoff.links) || {});
}

// `handoff.links` is a dict keyed by assistant name (e.g. {claude, codex,
// gemini}), not an array of {label, url} — the engine mixes real `claude://`
// URIs with plain CLI one-liner strings for codex/gemini (design.md Decision
// 7). Render a URI as a link; render a shell one-liner as copyable text.
function renderHandoffLinks(links) {
  const linksNode = byId("adopt-links");
  if (!linksNode) return;
  const entries = Object.entries(links || {});
  replaceChildren(linksNode, entries.map(([name, value]) => {
    const text = String(value || "");
    const isUri = /^[a-z][a-z0-9+.-]*:\/\//i.test(text) && !text.includes(" ");
    const label = name.charAt(0).toUpperCase() + name.slice(1);
    if (isUri) {
      const anchor = document.createElement("a");
      anchor.href = text;
      anchor.textContent = `Continue in ${label}`;
      anchor.className = "secondary link-button";
      anchor.rel = "noopener";
      return anchor;
    }
    const block = el("div", "", "cli-oneliner");
    block.append(el("span", `${label}: `, "fine-print"));
    block.append(el("code", text));
    const copy = el("button", "Copy", "link-button");
    copy.type = "button";
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(text);
        setStatus("Copied ✓");
      } catch (_error) {
        setStatus("Select the text and copy it (Ctrl/Cmd+C).");
      }
    });
    block.append(copy);
    return block;
  }));
  linksNode.hidden = !entries.length;
}

async function copyPrompt() {
  const textarea = byId("adopt-prompt");
  if (!textarea) return;
  textarea.focus();
  textarea.select();
  try {
    await navigator.clipboard.writeText(textarea.value);
    setStatus("Copied ✓");
  } catch (_error) {
    // Fallback: the text is already selected for a manual copy.
    setStatus("Select the text above and copy it (Ctrl/Cmd+C).");
  }
}

function waitForProposals() {
  const wait = byId("adopt-handoff-wait");
  if (wait) {
    wait.hidden = false;
    wait.textContent = "Waiting for suggestions… checking every few seconds. You can leave this page — we'll pick up where you left off.";
  }
  goStep("suggestions");
}

// --- Screen 8: Review suggestions ------------------------------------------- //

let proposals = null;

async function populateProposals() {
  setStatus("");
  try {
    proposals = await adoptionApi.proposals(run.run_ref);
  } catch (error) {
    showAdoptError(error);
    return;
  }
  const items = flattenProposals(proposals);
  const list = byId("adopt-proposal-list");
  if (list) {
    replaceChildren(list, items.map((item) => {
      const li = el("li");
      const button = el("button", "", "review-card");
      button.type = "button";
      button.append(el("strong", item.title || item.ref));
      button.addEventListener("click", () => openProposalDetail(item));
      li.append(button);
      return li;
    }));
    if (!items.length) {
      list.append(el("li", "No suggestions to review.", "section-note empty"));
    }
  }
}

function flattenProposals(data) {
  if (!data) return [];
  if (Array.isArray(data.items)) return data.items;
  if (Array.isArray(data.groups)) return data.groups.flatMap((group) => group.items || []);
  return [];
}

// Kind-aware rendering of the proposal's actual payload: the reviewer must be
// able to inspect exactly what will be written before approval is enabled.
function proposalSummary(context, item) {
  const payload = context.payload || {};
  const rows = [];
  if (payload.title) rows.push(el("strong", payload.title));
  if (payload.relation_type || context.kind === "relation") {
    const from = payload.from || payload.subject_path || "";
    const to = payload.to || payload.duplicate_of || "";
    if (from || to) rows.push(el("p", `${payload.relation_type || "relates_to"}: ${from} → ${to}`));
  }
  if (payload.name) rows.push(el("p", `${payload.entity_type || "entity"}: ${payload.name}`));
  if (payload.summary) rows.push(el("p", payload.summary, "fine-print"));
  if (payload.content) rows.push(el("pre", payload.content, "note-body"));
  const target = context.target || {};
  if (target.excerpt) {
    rows.push(el("h3", "Current target"), el("pre", target.excerpt, "note-body"));
  }
  if (!rows.length) rows.push(el("p", item.title || "Suggested change."));
  return rows;
}

async function openProposalDetail(item) {
  const detail = byId("adopt-proposal-detail");
  if (!detail) return;
  replaceChildren(detail, [el("p", "Loading suggestion…", "section-note")]);
  try {
    const context = await adoptionApi.proposalContext(item.ref, item.fingerprint);
    replaceChildren(detail, [el("h3", "What it wants to do"), ...proposalSummary(context, item)]);
    const actions = el("div", "", "actions");
    const approve = el("button", "Make this change");
    approve.type = "button";
    approve.addEventListener("click", () => approveProposal(item, context));
    const reject = el("button", "No thanks", "secondary");
    reject.type = "button";
    reject.addEventListener("click", () => rejectProposal(item));
    actions.append(approve, reject);
    detail.append(actions);
  } catch (error) {
    if (isDrift(error)) {
      showAdoptError(new ApiError("This suggestion is out of date — the files changed since it was made. Nothing was changed; the list has been refreshed."));
      await populateProposals();
      return;
    }
    showAdoptError(error);
  }
}

function approveProposal(item, context) {
  const expectedHash = ((context || {}).target || {}).content_hash || null;
  openConfirm({
    kicker: "SUGGESTIONS FROM YOUR ASSISTANT",
    title: "Make this change?",
    description: item.title || "Apply this suggestion. You can review the result afterwards.",
    confirmLabel: "Make this change",
    onConfirm: async () => {
      try {
        await adoptionApi.approveProposal(item.ref, item.fingerprint, "Approved from adoption review", expectedHash);
        await populateProposals();
      } catch (error) {
        if (isDrift(error)) {
          await populateProposals();
          throw new ApiError("This suggestion is out of date — nothing was changed; the list has been refreshed.");
        }
        throw error;
      }
    },
  });
}

async function rejectProposal(item) {
  try {
    await adoptionApi.rejectProposal(item.ref, item.fingerprint, null);
    await populateProposals();
  } catch (error) {
    if (isDrift(error)) {
      await populateProposals();
      return;
    }
    showAdoptError(error);
  }
}

// --- Screen 9: First question ----------------------------------------------- //

function populateQuestion() {
  setStatus("");
  const chips = suggestionChips((run && run.pack_suggestions) || [], topFolders());
  const chipsNode = byId("adopt-chips");
  if (chipsNode) {
    replaceChildren(chipsNode, chips.map((chip) => {
      const button = el("button", chip.label, "chip");
      button.type = "button";
      button.addEventListener("click", () => {
        byId("adopt-ask").value = chip.query;
        doAsk();
      });
      return button;
    }));
  }
}

async function doAsk() {
  const input = byId("adopt-ask");
  const query = input ? input.value.trim() : "";
  if (!query) return;
  const answers = byId("adopt-answers");
  replaceChildren(answers, [el("p", "Asking…", "section-note")]);
  try {
    const result = await adoptionApi.ask(query);
    const hits = (result && (result.hits || result.items)) || [];
    if (!hits.length) {
      replaceChildren(answers, [el("p", "Nothing matched yet. Try different words — or give Exomem a minute to finish settling in, then ask again.", "section-note empty")]);
      return;
    }
    replaceChildren(answers, hits.map((hit) => {
      const li = el("div", "", "answer");
      li.append(el("strong", hit.title || hit.path));
      if (hit.excerpt) li.append(el("p", hit.excerpt, "fine-print"));
      if (hit.path) li.append(el("p", `from ${hit.path}`, "path"));
      return li;
    }));
  } catch (error) {
    showAdoptError(error);
  }
}

// --- Terminal cards --------------------------------------------------------- //

function populateCancelled() {
  const node = byId("adopt-cancelled-text");
  if (node) {
    const applied = outcomesToResult().copied.length;
    node.textContent = applied
      ? `Stopped. The ${applied} files already copied are safe; your originals are untouched either way.`
      : "Stopped. Nothing was copied or changed.";
  }
}

function populateFailed() {
  const node = byId("adopt-failed-text");
  if (node) {
    const reason = (run && run.error && run.error.reason) || "";
    node.textContent = `We couldn't finish. ${reason ? reason + ". " : ""}Nothing was changed — your originals are untouched.`;
  }
}

// --- Polling ---------------------------------------------------------------- //

function isTransientPhase(phase) {
  return phase === "applying" || phase === "created" || phase === "scanning";
}

function isAwaitingProposals(screen) {
  return screen === "proposals" || screen === "handoff";
}

function managePolling(screen) {
  stopPolling();
  if (document.hidden) return;
  if (run && isTransientPhase(run.phase)) {
    schedulePoll("applying");
  } else if (isAwaitingProposals(screen) && run) {
    schedulePoll("awaiting_proposals");
  }
}

function schedulePoll(kind) {
  if (!pollStart) pollStart = Date.now();
  const delay = pollDelayFor(kind);
  pollTimer = setTimeout(async () => {
    if (document.hidden || !run) return;
    try {
      if (kind === "awaiting_proposals") {
        await populateProposals();
      } else {
        await refreshStatus();
        render();
        return;
      }
    } catch (_error) {
      // Keep polling; a transient error must not strand the run.
    }
    schedulePoll(kind);
  }, delay);
}

function pollDelayFor(kind) {
  const elapsed = pollStart ? Date.now() - pollStart : 0;
  return pollDelay(kind, elapsed);
}

async function refreshStatus() {
  if (!run || !run.run_id) return;
  run = await adoptionApi.status(run.run_id);
}

// --- Event wiring (once) ---------------------------------------------------- //

function wireOnce() {
  if (wired) return;
  wired = true;

  byId("adopt-start-scan")?.addEventListener("click", doStart);
  byId("adopt-resume-continue")?.addEventListener("click", () => render());
  byId("adopt-resume-restart")?.addEventListener("click", () => {
    run = null;
    selection = null;
    route = routePatch(route, {run: "", astep: "start"});
    writeRoute(route);
    render();
  });
  byId("adopt-scan-cancel")?.addEventListener("click", cancelScan);
  byId("adopt-to-choose")?.addEventListener("click", () => goStep("choose"));
  byId("adopt-rescan")?.addEventListener("click", recheckFolder);
  byId("adopt-not-now")?.addEventListener("click", () => leaveToReview());
  byId("adopt-choose-back")?.addEventListener("click", () => goStep("findings"));
  byId("adopt-to-preview")?.addEventListener("click", enterPreview);
  byId("adopt-junk-toggle")?.addEventListener("change", (event) => {
    selection = {...selection, includeJunk: event.target.checked};
    renderChooseStatus();
  });
  byId("adopt-apply")?.addEventListener("click", confirmApply);
  byId("adopt-preview-back")?.addEventListener("click", () => goStep("choose"));
  byId("adopt-recheck")?.addEventListener("click", recheckFolder);
  byId("adopt-apply-cancel")?.addEventListener("click", confirmStopApply);
  byId("adopt-retry")?.addEventListener("click", (event) => {
    let paths = [];
    try {
      paths = JSON.parse(event.currentTarget.dataset.paths || "[]");
    } catch (_error) {
      paths = [];
    }
    doRetry(paths);
  });
  byId("adopt-to-handoff")?.addEventListener("click", () => goStep("organize"));
  byId("adopt-skip-handoff")?.addEventListener("click", () => goStep("question"));
  byId("adopt-skip-handoff2")?.addEventListener("click", () => goStep("question"));
  byId("adopt-copy")?.addEventListener("click", copyPrompt);
  byId("adopt-wait")?.addEventListener("click", waitForProposals);
  byId("adopt-proposals-back")?.addEventListener("click", () => goStep("organize"));
  byId("adopt-proposals-finish")?.addEventListener("click", () => goStep("question"));
  byId("adopt-ask-btn")?.addEventListener("click", doAsk);
  byId("adopt-go-review")?.addEventListener("click", () => leaveToReview());
  byId("adopt-cancel-restart")?.addEventListener("click", restart);
  byId("adopt-failed-retry")?.addEventListener("click", restart);
  byId("adopt-unknown-restart")?.addEventListener("click", restart);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopPolling();
    else if (route && route.view === "adopt") managePolling(currentScreen());
  });
}

function currentScreen() {
  return run ? phaseScreen(run, route.astep) : "start";
}

function cancelScan() {
  if (run && run.run_id) {
    adoptionApi.cancel(run.run_id, "user stopped scan").catch(() => {});
  }
  run = null;
  selection = null;
  route = routePatch(route, {run: "", astep: "start"});
  writeRoute(route, {replace: true});
  showScreen("cancelled");
  const node = byId("adopt-cancelled-text");
  if (node) node.textContent = "Stopped. Nothing was copied or changed.";
}

function restart() {
  run = null;
  selection = null;
  route = routePatch(route, {run: "", astep: "start"});
  writeRoute(route);
  render();
}

function leaveToReview() {
  stopPolling();
  route = routePatch(route, {view: "review", run: "", astep: "start"});
  writeRoute(route);
  document.dispatchEvent(new CustomEvent("adopt:leave"));
}

function wireRovingTabindex(container) {
  container.addEventListener("keydown", (event) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    // Disabled boxes (non-importable files) can't take focus — skip them so
    // arrowing never dead-ends and silently drops keyboard focus.
    const inputs = [...container.querySelectorAll("input[type=checkbox]:not(:disabled)")];
    const current = inputs.indexOf(document.activeElement);
    let next = event.key === "End" ? inputs.length - 1 : 0;
    if (event.key === "ArrowDown") next = Math.min(inputs.length - 1, current + 1);
    if (event.key === "ArrowUp") next = Math.max(0, current - 1);
    for (const input of inputs) input.tabIndex = -1;
    if (inputs[next]) {
      inputs[next].tabIndex = 0;
      inputs[next].focus();
    }
    event.preventDefault();
  });
}

function cssId(path) {
  return String(path).replace(/[^a-zA-Z0-9_-]/g, "_");
}

function isStale(error) {
  return error instanceof ApiError
    && (error.code === "ADOPTION_SOURCE_CHANGED" || error.code === "PLAN_STALE");
}

function isDrift(error) {
  return error instanceof ApiError && error.code === "REVIEW_ITEM_CHANGED";
}

// --- Entry point (called by app.v4.js) -------------------------------------- //

export async function enter(incoming) {
  wireOnce();
  route = incoming || readRoute();
  stopPolling();
  pollStart = 0;
  if (route.run) {
    if (!run || run.run_id !== route.run) {
      try {
        run = await adoptionApi.status(route.run);
        // Resume with the persisted rules; a fresh default (all-on) would
        // silently overwrite the user's exclusions on the next preview.
        selection = selectionFromRules(
          (run.selection || {}).rules,
          inventoryRows().map((row) => row.path),
        );
      } catch (error) {
        run = null;
        showScreen("start");
        if (error instanceof ApiError && error.code === "RUN_NOT_FOUND") {
          showAdoptError(new ApiError("This session expired. Start again — nothing was lost."));
        } else {
          showAdoptError(error);
        }
        return;
      }
    }
    render();
  } else {
    run = null;
    selection = null;
    showScreen("start");
    populateStart();
    try {
      const latest = await adoptionApi.latest();
      if (latest && latest.run_id) {
        const resume = byId("adopt-resume");
        const text = byId("adopt-resume-text");
        if (text) text.textContent = "Pick up where you left off.";
        if (resume) resume.hidden = false;
      }
    } catch (_error) {
      // Resume discovery is best-effort.
    }
  }
}

export function leave() {
  stopPolling();
}
