import {ApiError, command, setStoredKey, storedKey} from "/studio/assets/api.v1.js";
import {categoriesFor, reportStatus, sectionState, visibleItems} from "/studio/assets/model.v1.js";
import {readRoute, routePatch, writeRoute} from "/studio/assets/state.v1.js";

const byId = (id) => document.getElementById(id);
const authPanel = byId("auth-panel");
const studio = byId("studio");
const reviewList = byId("review-list");
const status = byId("worklist-status");
const workspaceError = byId("workspace-error");
const dialog = byId("action-dialog");
let route = readRoute();
let report = null;
let context = null;
let dialogAction = null;
let restoreRef = "";

function element(tag, text = "", className = "") {
  const node = document.createElement(tag);
  if (text !== "") node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function replaceChildren(target, children = []) {
  target.replaceChildren(...children);
}

function label(value) {
  return String(value || "unknown").replaceAll("_", " ");
}

function errorMessage(error) {
  if (!(error instanceof ApiError)) return "Unexpected Studio error.";
  return error.remediation ? `${error.message} ${error.remediation}` : error.message;
}

function showError(target, error) {
  target.textContent = errorMessage(error);
  target.hidden = false;
  target.focus?.();
}

function connected(value) {
  const node = byId("connection-status");
  node.textContent = value ? "Authenticated REST session" : "Not connected";
  node.classList.toggle("connected", value);
}

function showStudio() {
  authPanel.hidden = true;
  studio.hidden = false;
  connected(true);
}

function showAuth(error = null) {
  studio.hidden = true;
  authPanel.hidden = false;
  connected(false);
  if (error) showError(byId("auth-error"), error);
}

async function authenticate(key = "") {
  byId("auth-error").hidden = true;
  try {
    const data = await command("review_memory", {mode: route.mode, state: route.state, limit: 50}, {key});
    if (key) setStoredKey(key);
    report = data;
    showStudio();
    renderWorklist();
    await restoreSelection();
  } catch (error) {
    if (key) setStoredKey("");
    showAuth(error);
  }
}

async function loadWorklist({focusRef = ""} = {}) {
  status.textContent = route.mode === "activation" ? "Measuring corpus activation…" : "Loading Inbox…";
  try {
    report = await command("review_memory", {mode: route.mode, state: route.state, limit: 50});
    renderWorklist();
    if (route.ref) await restoreSelection();
    if (focusRef) focusItem(focusRef);
  } catch (error) {
    if (error instanceof ApiError && (error.status === 401 || error.code === "REST_DISABLED")) {
      showAuth(error);
      return;
    }
    report = null;
    replaceChildren(reviewList);
    status.textContent = `Worklist unavailable: ${errorMessage(error)}`;
  }
}

function renderWorklist() {
  const activation = route.mode === "activation";
  byId("worklist-kicker").textContent = activation ? "Opt-in structural backlog" : "Daily review";
  byId("worklist-title").textContent = activation ? "Corpus Activation" : "Epistemic Inbox";
  for (const tab of document.querySelectorAll("[data-mode]")) {
    tab.setAttribute("aria-selected", String(tab.dataset.mode === route.mode));
  }
  byId("state-filter").value = route.state;
  renderCategoryOptions();
  renderCoverage();
  const items = visibleItems(report, route.category);
  status.textContent = reportStatus(report, items.length);
  if (report?.note) status.textContent += ` · ${report.note}`;
  replaceChildren(reviewList, items.map(renderReviewItem));
  if (!items.length) {
    const empty = element("li", activation ? "No activation findings in this view." : "Nothing needs attention in this view.", "section-note empty");
    reviewList.append(empty);
  }
}

function renderCategoryOptions() {
  const select = byId("category-filter");
  const options = [new Option("All categories", "")];
  for (const category of categoriesFor(report)) options.push(new Option(label(category), category));
  replaceChildren(select, options);
  select.value = options.some((option) => option.value === route.category) ? route.category : "";
  if (select.value !== route.category) {
    route = routePatch(route, {category: ""});
    writeRoute(route, {replace: true});
  }
}

function renderCoverage() {
  const node = byId("coverage");
  const coverage = report?.coverage;
  if (route.mode !== "activation" || !coverage) {
    node.hidden = true;
    replaceChildren(node);
    return;
  }
  const rows = [];
  for (const [name, value] of Object.entries(coverage)) {
    const row = document.createElement("div");
    row.append(element("dt", label(name)), element("dd", value));
    rows.push(row);
  }
  replaceChildren(node, rows);
  node.hidden = false;
}

function renderReviewItem(item) {
  const li = document.createElement("li");
  const button = element("button", "", "review-card");
  button.type = "button";
  button.dataset.ref = item.ref;
  button.setAttribute("aria-current", String(item.ref === route.ref));
  button.append(element("strong", item.title || item.path || "Untitled review item"));
  const meta = element("span", "", `review-meta severity-${item.severity || "info"}`);
  meta.append(element("span", label(item.state || "open"), "tag"));
  for (const category of item.categories || []) meta.append(element("span", label(category), "tag"));
  button.append(meta);
  const reason = item.reasons?.[0]?.detail;
  if (reason) button.append(element("span", reason, "fine-print"));
  button.addEventListener("click", () => selectItem(item));
  li.append(button);
  return li;
}

async function selectItem(item, {push = true} = {}) {
  route = routePatch(route, {ref: item.ref});
  if (push) writeRoute(route);
  for (const card of document.querySelectorAll(".review-card")) {
    card.setAttribute("aria-current", String(card.dataset.ref === item.ref));
  }
  byId("workspace-empty").hidden = true;
  byId("workspace-content").hidden = true;
  workspaceError.hidden = true;
  workspaceError.textContent = "";
  status.textContent = "Loading bounded review context…";
  try {
    context = await command("review_item_context", {ref: item.ref, expected_fingerprint: item.fingerprint});
    renderContext(context);
    status.textContent = reportStatus(report, visibleItems(report, route.category).length);
  } catch (error) {
    if (error instanceof ApiError && error.code === "REVIEW_ITEM_CHANGED") {
      route = routePatch(route, {ref: ""});
      writeRoute(route, {replace: true});
      showError(workspaceError, new ApiError("This review signal changed. The worklist has been refreshed; inspect the current item before acting.", {code: error.code}));
      await loadWorklist();
      return;
    }
    showError(workspaceError, error);
  }
}

async function restoreSelection() {
  if (!route.ref) return;
  const item = (report?.items || []).find((candidate) => candidate.ref === route.ref);
  if (!item) {
    route = routePatch(route, {ref: ""});
    writeRoute(route, {replace: true});
    byId("workspace-content").hidden = true;
    byId("workspace-empty").hidden = false;
    status.textContent = `${reportStatus(report, visibleItems(report, route.category).length)} · Prior selection is no longer in this view.`;
    return;
  }
  await selectItem(item, {push: false});
}

function renderContext(data) {
  const item = data.item || {};
  const target = data.target || {};
  byId("item-state").textContent = `State: ${label(item.state)}`;
  byId("item-title").textContent = target.title || target.path || "Review item";
  byId("item-path").textContent = target.ref ? `${target.path} · ${target.ref}` : target.path || "";
  replaceChildren(byId("item-reasons"), (item.reasons || []).map((reason) => {
    const node = element("div", reason.detail || label(reason.category), "reason");
    if (reason.category) node.prepend(element("strong", `${label(reason.category)} · `));
    return node;
  }));
  byId("target-body").textContent = target.body || "No readable body was returned.";
  if (target.body_truncated) byId("target-body").append("\n\nTarget body was truncated by the requested bound.");
  renderRelated(data.related);
  renderProvenance(data.provenance);
  renderGraph(data.graph);
  renderHistory(data.history);
  renderEvolution(data.evolution);
  setPanel(route.panel);
  byId("workspace-empty").hidden = true;
  byId("workspace-content").hidden = false;
  byId("workspace").focus();
}

function stateNote(section, emptyText) {
  const state = sectionState(section);
  if (state === "available") return null;
  let text = emptyText;
  if (state === "unavailable") text = section?.reason || "This section is unavailable.";
  if (state === "truncated") text = `${emptyText} Some recorded results were omitted by the response bound.`;
  return element("p", text, `section-note ${state}`);
}

function renderRelated(section = {}) {
  const rows = (section.items || []).map((row) => {
    const li = element("li");
    li.append(element("strong", row.title || row.path));
    li.append(element("p", row.excerpt || "No excerpt returned.", "fine-print"));
    li.append(element("code", row.ref || row.path));
    return li;
  });
  const note = stateNote({...section, pages: section.items}, "No bounded related pages were recorded.");
  replaceChildren(byId("related-pages"), [note, rows.length ? list(rows) : null].filter(Boolean));
}

function renderProvenance(section = {}) {
  const rows = [];
  for (const kind of ["sources", "evidence"]) {
    for (const row of section[kind] || []) {
      const li = element("li");
      li.append(element("strong", `${label(kind.slice(0, -1))}: `), document.createTextNode(row.path || "Unknown"));
      li.append(element("code", ` ${row.ref || ""}`));
      rows.push(li);
    }
  }
  const note = stateNote({...section, items: rows}, "No recorded source or evidence links.");
  replaceChildren(byId("evidence"), [note, rows.length ? list(rows) : null].filter(Boolean));
}

function renderGraph(section = {}) {
  const rows = [];
  for (const node of section.nodes || []) {
    rows.push(element("li", `Node · ${node.title || node.path || node.node_key} · ${node.ref || "no canonical reference"}`));
  }
  for (const edge of section.edges || []) {
    const source = edge.source_ref ? ` · ${edge.source_ref}` : "";
    rows.push(element("li", `${edge.src_key} — ${edge.relation_type || edge.raw_relation || "related"} → ${edge.dst_key}${source}`));
  }
  const note = stateNote({...section, items: rows, truncated: section.truncated_edges || section.truncated_nodes}, "No recorded graph neighborhood.");
  replaceChildren(byId("graph"), [note, rows.length ? list(rows) : null].filter(Boolean));
}

function renderHistory(section = {}) {
  const rows = (section.items || []).map((row) => element("li", [row.date, row.op, row.summary].filter(Boolean).join(" · ")));
  const note = stateNote({...section, entries: section.items}, "No matching audit-log history.");
  replaceChildren(byId("history"), [note, rows.length ? list(rows) : null].filter(Boolean));
}

function renderEvolution(section = {}) {
  const target = byId("evolution-list");
  if (section.available === false) {
    replaceChildren(target, [element("li", section.reason || "Evolution is unavailable.", "section-note unavailable")]);
    return;
  }
  const timelines = section.timelines || [];
  if (!timelines.length) {
    replaceChildren(target, [element("li", "No recorded supersession evolution exists for this target.", "section-note empty")]);
    return;
  }
  const rows = [];
  for (const timeline of timelines) {
    for (const version of timeline.versions || []) {
      const li = element("li");
      li.tabIndex = 0;
      li.append(element("strong", version.title || version.path));
      li.append(element("p", [version.date, version.status, version.path, version.ref].filter(Boolean).join(" · "), "fine-print"));
      for (const claim of version.claims || []) li.append(element("p", String(claim)));
      if (version.transition) {
        li.append(element("p", `Recorded transition: ${version.transition.reason || "No reason stored"}${version.transition.date ? ` · ${version.transition.date}` : ""}`, "reason"));
      }
      rows.push(li);
    }
  }
  for (const note of section.truncation || []) rows.push(element("li", note, "section-note truncated"));
  replaceChildren(target, rows);
}

function list(rows) {
  const ul = element("ul", "", "record-list");
  ul.append(...rows);
  return ul;
}

function setPanel(panel, {push = false} = {}) {
  route = routePatch(route, {panel});
  if (push) writeRoute(route);
  byId("context-panel").hidden = panel !== "context";
  byId("evolution-panel").hidden = panel !== "evolution";
  for (const button of document.querySelectorAll("[data-panel]")) {
    button.setAttribute("aria-pressed", String(button.dataset.panel === panel));
  }
}

function focusItem(ref) {
  const card = [...document.querySelectorAll(".review-card")].find((node) => node.dataset.ref === ref);
  card?.focus();
}

function openTriage(action) {
  if (!context) return;
  dialogAction = {kind: "triage", action};
  restoreRef = context.item.ref;
  byId("dialog-kicker").textContent = "Governed triage";
  byId("dialog-title").textContent = `${label(action)} this review signal?`;
  byId("dialog-description").textContent = `${context.target.title || context.target.path} · ${context.item.ref}`;
  const fields = [];
  if (action === "snooze") {
    const labelNode = element("label", "Snooze through");
    const input = document.createElement("input");
    input.name = "until";
    input.type = "date";
    input.required = true;
    labelNode.append(input);
    fields.push(labelNode);
  }
  const whyLabel = element("label", "Optional rationale");
  const why = document.createElement("textarea");
  why.name = "why";
  why.rows = 3;
  whyLabel.append(why);
  fields.push(whyLabel);
  replaceChildren(byId("dialog-fields"), fields);
  byId("dialog-error").hidden = true;
  byId("dialog-confirm").textContent = `Confirm ${label(action)}`;
  dialog.showModal();
}

function inputField(labelText, name, value = "", {required = false, multiline = false} = {}) {
  const wrapper = element("label", labelText);
  const control = document.createElement(multiline ? "textarea" : "input");
  control.name = name;
  control.value = value || "";
  control.required = required;
  if (multiline) control.rows = 10;
  wrapper.append(control);
  return wrapper;
}

async function openProposal(kind) {
  if (!context) return;
  restoreRef = context.item.ref;
  workspaceError.hidden = true;
  byId("dialog-error").hidden = true;
  byId("dialog-kicker").textContent = "Read-only proposal first";
  byId("dialog-confirm").disabled = true;
  replaceChildren(byId("dialog-fields"), [element("p", "Preparing a bounded proposal…", "section-note")]);
  dialog.showModal();
  try {
    let ready = true;
    if (kind === "relation") ready = await prepareRelationProposal();
    if (kind === "compile") ready = await prepareCompileProposal();
    if (kind === "replace") ready = prepareReplacePreview();
    byId("dialog-confirm").disabled = !ready;
  } catch (error) {
    dialogAction = null;
    showError(byId("dialog-error"), error);
  }
}

async function prepareRelationProposal() {
  const proposal = await command("connect_memory", {
    operation: "suggest-relations",
    path: context.target.path,
    include_model_suggestions: true,
    limit: 10,
  });
  dialogAction = {kind: "relation", proposal};
  byId("dialog-title").textContent = "Review a provisional relation";
  byId("dialog-description").textContent = `${context.target.title || context.target.path} · Suggestions are read-only and may include model-backed candidates. Choose one, then confirm a separate audited edit.`;
  const candidates = proposal.candidates || [];
  if (!candidates.length) {
    replaceChildren(byId("dialog-fields"), [element("p", "No relation candidates were measured. Nothing can be written from this proposal.", "section-note empty")]);
    return false;
  }
  const wrapper = element("label", "Provisional candidate");
  const select = document.createElement("select");
  select.name = "candidate";
  candidates.forEach((candidate, index) => {
    const modelLabel = candidate.method === "model" ? " · model-backed proposal" : "";
    select.append(new Option(`${candidate.relation_type || "relates_to"} → ${candidate.to}${modelLabel}`, String(index)));
  });
  wrapper.append(select);
  const warnings = (proposal.warnings || []).map((warning) => element("p", String(warning), "section-note"));
  replaceChildren(byId("dialog-fields"), [wrapper, inputField("Audit reason", "why", "Accepted reviewed relation", {required: true}), ...warnings]);
  byId("dialog-confirm").textContent = "Confirm governed edit";
  return true;
}

async function prepareCompileProposal() {
  const proposal = await command("compile_source", {sources: [context.target.path]});
  dialogAction = {kind: "compile", proposal};
  byId("dialog-title").textContent = "Review compiled-knowledge draft";
  byId("dialog-description").textContent = `${context.target.title || context.target.path} remains unchanged. Edit this read-only proposal; only confirmation creates a governed note.`;
  replaceChildren(byId("dialog-fields"), [
    inputField("Title", "title", proposal.suggested_title || context.target.title, {required: true}),
    inputField("Note type", "note_type", proposal.suggested_note_type || "insight", {required: true}),
    inputField("Project key (required for research-note)", "project", ""),
    inputField("Editable compiled draft", "content", proposal.outline_markdown || "", {required: true, multiline: true}),
  ]);
  byId("dialog-confirm").textContent = "Confirm create knowledge";
  return true;
}

function prepareReplacePreview() {
  dialogAction = {kind: "replace"};
  byId("dialog-title").textContent = "Preview a superseding conclusion";
  byId("dialog-description").textContent = `Target: ${context.target.title || context.target.path}. Confirmation will create a successor and mark this exact page superseded; cancellation writes nothing.`;
  replaceChildren(byId("dialog-fields"), [
    inputField("Successor title", "title", context.target.title, {required: true}),
    inputField("Note type", "note_type", context.target.type || "insight", {required: true}),
    inputField("Recorded reason for supersession", "reason", "", {required: true}),
    inputField("Successor draft", "content", context.target.body || "", {required: true, multiline: true}),
  ]);
  byId("dialog-confirm").textContent = "Confirm supersession";
  return true;
}

async function submitDialog(event) {
  event.preventDefault();
  if (!dialogAction) return;
  if (dialogAction.kind === "triage") await submitTriage();
  if (dialogAction.kind === "relation") await submitRelation();
  if (dialogAction.kind === "compile") await submitCompilation();
  if (dialogAction.kind === "replace") await submitReplacement();
}

async function submitTriage() {
  const confirm = byId("dialog-confirm");
  confirm.disabled = true;
  const data = new FormData(byId("dialog-form"));
  try {
    await command("review_item_context", {ref: context.item.ref, expected_fingerprint: context.item.fingerprint});
    await command("triage_memory", {
      ref: context.item.ref,
      action: dialogAction.action,
      until: data.get("until") || null,
      why: data.get("why") || null,
    });
    dialog.close();
    context = null;
    route = routePatch(route, {ref: ""});
    writeRoute(route, {replace: true});
    byId("workspace-content").hidden = true;
    byId("workspace-empty").hidden = false;
    await loadWorklist({focusRef: restoreRef});
  } catch (error) {
    if (error instanceof ApiError && error.code === "REVIEW_ITEM_CHANGED") {
      showError(byId("dialog-error"), new ApiError("The review signal changed. Nothing was written; refresh and inspect the current context."));
      await loadWorklist();
    } else showError(byId("dialog-error"), error);
  } finally {
    confirm.disabled = false;
  }
}

async function guardedWrite(write) {
  const confirm = byId("dialog-confirm");
  confirm.disabled = true;
  try {
    await command("review_item_context", {ref: context.item.ref, expected_fingerprint: context.item.fingerprint});
    await write();
    dialog.close();
    context = null;
    route = routePatch(route, {ref: ""});
    writeRoute(route, {replace: true});
    byId("workspace-content").hidden = true;
    byId("workspace-empty").hidden = false;
    await loadWorklist({focusRef: restoreRef});
  } catch (error) {
    if (error instanceof ApiError && error.code === "REVIEW_ITEM_CHANGED") {
      showError(byId("dialog-error"), new ApiError("The reviewed signal changed. The draft is preserved and nothing was written; refresh before confirming."));
      await loadWorklist();
    } else showError(byId("dialog-error"), error);
  } finally {
    confirm.disabled = false;
  }
}

async function submitRelation() {
  const data = new FormData(byId("dialog-form"));
  const candidate = dialogAction.proposal.candidates?.[Number(data.get("candidate"))];
  if (!candidate) {
    showError(byId("dialog-error"), new ApiError("Choose a valid proposal before confirming."));
    return;
  }
  await guardedWrite(() => command("edit_memory", {
    path: context.target.path,
    why: data.get("why"),
    heading: "Relations",
    section_position: "append",
    new_string: `- ${candidate.relation_type || "relates_to"} [[${String(candidate.to || "").replace(/\.md$/, "")}]]`,
    expected_hash: context.target.content_hash,
  }));
}

async function submitCompilation() {
  const data = new FormData(byId("dialog-form"));
  await guardedWrite(() => command("remember", {
    title: data.get("title"),
    note_type: data.get("note_type"),
    project: data.get("project") || null,
    content: data.get("content"),
    sources: [context.target.path],
    suggestions: true,
  }));
}

async function submitReplacement() {
  const data = new FormData(byId("dialog-form"));
  await guardedWrite(() => command("replace_memory", {
    old_path: context.target.path,
    title: data.get("title"),
    note_type: data.get("note_type"),
    reason: data.get("reason"),
    content: data.get("content"),
    sources: context.target.frontmatter?.sources || null,
  }));
}

function wireEvents() {
  byId("auth-form").addEventListener("submit", (event) => {
    event.preventDefault();
    authenticate(byId("api-key").value);
  });
  byId("access-connect").addEventListener("click", () => authenticate(""));
  byId("refresh").addEventListener("click", () => loadWorklist({focusRef: route.ref}));
  for (const tab of document.querySelectorAll("[data-mode]")) {
    tab.addEventListener("click", async () => {
      route = routePatch(route, {mode: tab.dataset.mode, category: "", ref: ""});
      writeRoute(route);
      byId("workspace-content").hidden = true;
      byId("workspace-empty").hidden = false;
      await loadWorklist();
    });
  }
  byId("state-filter").addEventListener("change", async (event) => {
    route = routePatch(route, {state: event.target.value, ref: ""});
    writeRoute(route);
    await loadWorklist();
  });
  byId("category-filter").addEventListener("change", (event) => {
    route = routePatch(route, {category: event.target.value, ref: ""});
    writeRoute(route);
    renderWorklist();
  });
  reviewList.addEventListener("keydown", (event) => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    const cards = [...reviewList.querySelectorAll(".review-card")];
    const current = cards.indexOf(document.activeElement);
    let next = event.key === "End" ? cards.length - 1 : 0;
    if (event.key === "ArrowDown") next = Math.min(cards.length - 1, current + 1);
    if (event.key === "ArrowUp") next = Math.max(0, current - 1);
    cards[next]?.focus();
    event.preventDefault();
  });
  byId("evolution-list").addEventListener("keydown", (event) => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    const versions = [...byId("evolution-list").querySelectorAll("li[tabindex]")];
    const current = versions.indexOf(document.activeElement);
    let next = event.key === "End" ? versions.length - 1 : 0;
    if (event.key === "ArrowDown") next = Math.min(versions.length - 1, current + 1);
    if (event.key === "ArrowUp") next = Math.max(0, current - 1);
    versions[next]?.focus();
    event.preventDefault();
  });
  for (const button of document.querySelectorAll("[data-panel]")) {
    button.addEventListener("click", () => setPanel(button.dataset.panel, {push: true}));
  }
  for (const button of document.querySelectorAll("[data-triage]")) {
    button.addEventListener("click", () => openTriage(button.dataset.triage));
  }
  for (const button of document.querySelectorAll("[data-proposal]")) {
    button.addEventListener("click", () => openProposal(button.dataset.proposal));
  }
  byId("dialog-form").addEventListener("submit", submitDialog);
  byId("dialog-cancel").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => focusItem(restoreRef));
  window.addEventListener("popstate", async () => {
    route = readRoute();
    await loadWorklist();
  });
}

wireEvents();
if (storedKey()) authenticate(storedKey());
else showAuth();
