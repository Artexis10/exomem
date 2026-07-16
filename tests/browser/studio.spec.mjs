import {expect, test} from "@playwright/test";

const inboxItems = [
  {ref: "exomem://review/first", fingerprint: "fingerprint-first", target_ref: "exomem://vault/first", path: "Knowledge Base/Notes/Insights/first.md", title: "First measured conclusion", state: "open", severity: "warn", categories: ["stale_review"], reasons: [{category: "stale_review", detail: "Review date elapsed."}]},
  {ref: "exomem://review/second", fingerprint: "fingerprint-second", path: "Knowledge Base/Notes/Insights/second.md", title: "Second measured conclusion", state: "open", severity: "info", categories: ["relation_debt"], reasons: [{category: "relation_debt", detail: "No governed relation recorded."}]},
];
const activationItems = [{...inboxItems[1], ref: "exomem://review/activation", fingerprint: "fingerprint-activation", title: "Activation-only relation debt", categories: ["typed_relation_debt"]}];

function contextFor(item) {
  return {
    item,
    target: {path: item.path, ref: item.target_ref || "exomem://vault/first", title: item.title, type: "insight", status: "active", body: "# Recorded target\n\nA bounded measured claim.", body_truncated: false, content_hash: "content-hash", frontmatter: {type: "insight"}},
    related: {available: true, items: [{title: "Related", path: "related.md", ref: "exomem://vault/related", excerpt: "Recorded excerpt."}], shown: 1, total: 1, truncated: 0},
    provenance: {available: true, sources: [{path: "source.md", ref: "exomem://source/source", exists: true}], evidence: []},
    graph: {available: true, nodes: [{title: "Related", path: "related.md", ref: "exomem://vault/related", node_key: "file:related"}], edges: [], truncated_nodes: 0, truncated_edges: 0},
    history: {available: true, items: [{date: "2026-07-11", op: "edit", summary: "Recorded edit"}], truncated: 0},
    evolution: {available: true, truncation: [], timelines: [{versions: [
      {title: "Earlier conclusion", path: "old.md", ref: "exomem://vault/old", date: "2026-01-01", status: "superseded", claims: ["Earlier claim"], transition: {reason: "New evidence", date: "2026-06-01"}},
      {title: item.title, path: item.path, ref: item.target_ref, date: "2026-06-01", status: "active", claims: ["Current claim"], transition: null},
    ]}]},
    availability: {target: true, related: true, provenance: true, graph: true, history: true, evolution: true},
    truncation: [],
  };
}

async function mockApi(page, calls) {
  await page.route("**/api/*", async (route) => {
    const request = route.request();
    const name = new URL(request.url()).pathname.split("/").pop();
    const body = request.postDataJSON();
    calls.push({name, body, authorization: request.headers().authorization || ""});
    let data;
    if (name === "review_memory") {
      data = body.mode === "activation"
        ? {items: activationItems, shown: 1, total: 1, truncated: 0, coverage: {eligible_pages: 8, typed_relation_pages: 3}}
        : {items: inboxItems, shown: 2, total: 5, truncated: 3, upstream_truncated: 1, note: "3 more not shown"};
    } else if (name === "review_item_context") {
      const item = [...inboxItems, ...activationItems].find((candidate) => candidate.ref === body.ref) || inboxItems[0];
      data = contextFor(item);
    } else if (name === "connect_memory") {
      data = {mutated: false, warnings: [], candidates: [{from: inboxItems[0].path, to: "Knowledge Base/Notes/Insights/related.md", relation_type: "supports", method: "recorded-link"}]};
    } else {
      data = {state: body.action || "confirmed", path: inboxItems[0].path};
    }
    await route.fulfill({status: 200, contentType: "application/json", body: JSON.stringify({success: true, data})});
  });
}

async function connect(page, calls) {
  await mockApi(page, calls);
  await page.goto("/studio/");
  await page.getByLabel("REST bearer key").fill("browser-session-key");
  await page.getByRole("button", {name: "Connect", exact: true}).click();
  await expect(page.getByRole("heading", {name: "Epistemic Inbox"})).toBeVisible();
}

test("authenticated Inbox inspection, triage, evolution, and history navigation", async ({page}) => {
  const calls = [];
  await connect(page, calls);
  expect(calls[0].authorization).toBe("Bearer browser-session-key");
  await expect(page.getByText("3 omitted by the requested limit")).toBeVisible();
  await page.getByRole("button", {name: /First measured conclusion/}).click();
  // The card meta also carries the reason text, so scope to the workspace.
  await expect(page.locator("#item-reasons")).toContainText("Review date elapsed.");
  await page.getByRole("button", {name: "Evolution"}).click();
  await expect(page.getByText("Recorded transition: New evidence")).toBeVisible();
  await page.goBack();
  // One back-step returns from Evolution to the same item's Context panel.
  await expect(page.locator("#evolution-panel")).toBeHidden();
  await expect(page.getByRole("heading", {name: "Target"})).toBeVisible();
  await page.goForward();
  await expect(page.getByText("Earlier conclusion")).toBeVisible();
  await page.getByRole("button", {name: "Dismiss"}).click();
  await expect(page.getByRole("dialog")).toContainText("First measured conclusion");
  await page.getByRole("button", {name: "Confirm dismiss"}).click();
  await expect.poll(() => calls.some((call) => call.name === "triage_memory" && call.body.action === "dismiss")).toBeTruthy();
});

test("activation stays separate and governed relation requires confirmation", async ({page}) => {
  const calls = [];
  await connect(page, calls);
  await expect(page.getByText("Activation-only relation debt")).toHaveCount(0);
  await page.getByRole("tab", {name: "Activation"}).click();
  await expect(page.getByText("Activation-only relation debt")).toBeVisible();
  await expect(page.getByText("eligible pages")).toBeVisible();
  await page.getByRole("tab", {name: "Inbox"}).click();
  await page.getByRole("button", {name: /First measured conclusion/}).click();
  await page.getByRole("button", {name: "Suggest relation"}).click();
  await expect(page.getByRole("dialog")).toContainText("provisional relation");
  expect(calls.some((call) => call.name === "connect_memory")).toBeTruthy();
  expect(calls.some((call) => call.name === "edit_memory")).toBeFalsy();
  await page.getByRole("button", {name: "Confirm governed edit"}).click();
  await expect.poll(() => calls.some((call) => call.name === "edit_memory")).toBeTruthy();
});

test("narrow viewport and keyboard-only list navigation remain usable", async ({page}) => {
  const calls = [];
  await page.setViewportSize({width: 390, height: 844});
  await connect(page, calls);
  const first = page.getByRole("button", {name: /First measured conclusion/});
  const second = page.getByRole("button", {name: /Second measured conclusion/});
  await first.focus();
  await page.keyboard.press("ArrowDown");
  await expect(second).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", {name: "Second measured conclusion"})).toBeVisible();
});
