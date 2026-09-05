import {expect, test} from "@playwright/test";

const inboxItems = [
  {ref: "exomem://review/first", fingerprint: "fingerprint-first", target_ref: "exomem://vault/first", path: "Knowledge Base/Notes/Insights/first.md", title: "First measured conclusion", state: "open", severity: "warn", categories: ["stale_review"], reasons: [{category: "stale_review", detail: "Review date elapsed."}]},
  {ref: "exomem://review/second", fingerprint: "fingerprint-second", path: "Knowledge Base/Notes/Insights/second.md", title: "Second measured conclusion", state: "open", severity: "info", categories: ["relation_debt"], reasons: [{category: "relation_debt", detail: "No governed relation recorded."}]},
];
const activationItems = [{...inboxItems[1], ref: "exomem://review/activation", fingerprint: "fingerprint-activation", title: "Activation-only relation debt", categories: ["typed_relation_debt"]}];
const relationGroups = [
  {
    title: "Z source",
    path: "Knowledge Base/Notes/Insights/z-source.md",
    source_path: "Knowledge Base/Notes/Insights/z-source.md",
    content_hash: "hash-z",
    source_content_hash: "hash-z",
    items: [
      {ref: "exomem://review/relation/z-second", fingerprint: "fp-z-second", source_path: "Knowledge Base/Notes/Insights/z-source.md", source_content_hash: "hash-z", relation_type: "supports", to: "Knowledge Base/Notes/Insights/target-two.md", method: "body-wikilink"},
      {ref: "exomem://review/relation/z-first", fingerprint: "fp-z-first", source_path: "Knowledge Base/Notes/Insights/z-source.md", source_content_hash: "hash-z", relation_type: "part_of", to: "Knowledge Base/Notes/Insights/target-one.md", method: "shared-source"},
    ],
  },
  {
    title: "A source",
    path: "Knowledge Base/Notes/Insights/a-source.md",
    source_path: "Knowledge Base/Notes/Insights/a-source.md",
    content_hash: "hash-a",
    source_content_hash: "hash-a",
    items: [
      {ref: "exomem://review/relation/a-only", fingerprint: "fp-a-only", source_path: "Knowledge Base/Notes/Insights/a-source.md", source_content_hash: "hash-a", relation_type: "applies_to", to: "Knowledge Base/Notes/Insights/target-a.md", method: "frontmatter"},
    ],
  },
];

function relationQueue({status = "available", groups = relationGroups} = {}) {
  const shown = groups.flatMap((group) => group.items).length;
  return {
    mode: "relation-queue",
    status,
    groups: status === "available" ? groups : [],
    shown: status === "available" ? shown : 0,
    pages_shown: status === "available" ? groups.length : 0,
    pages_truncated: status === "available" ? 7 : 0,
    coverage: status === "available" ? {eligible_pages: 120, pages_with_candidates: 9} : null,
    retryable: status !== "available",
    next_action: status !== "available" ? "retry-relation-queue" : null,
  };
}

function governedResponseBarrier() {
  let markArrived;
  let releaseResponse;
  const arrived = new Promise((resolve) => { markArrived = resolve; });
  const released = new Promise((resolve) => { releaseResponse = resolve; });
  return {
    arrived,
    hold: async () => {
      markArrived();
      await released;
    },
    release: () => releaseResponse(),
  };
}

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

async function mockApi(page, calls, controls = {}) {
  controls.removedRefs ||= new Set();
  controls.relationQueueCalls ||= 0;
  await page.route("**/api/*", async (route) => {
    const request = route.request();
    const name = new URL(request.url()).pathname.split("/").pop();
    const body = request.postDataJSON();
    calls.push({name, body, authorization: request.headers().authorization || ""});
    let data;
    if (name === "review_memory") {
      if (body.mode === "relation-queue") {
        controls.relationQueueCalls += 1;
        const queued = controls.queueResponses?.length
          ? controls.queueResponses.shift()
          : relationQueue();
        data = {
          ...queued,
          groups: (queued.groups || []).map((group) => ({
            ...group,
            items: group.items.filter((item) => !controls.removedRefs.has(item.ref)),
          })).filter((group) => group.items.length),
        };
        data.shown = data.groups.flatMap((group) => group.items).length;
        data.pages_shown = data.groups.length;
      } else {
        data = body.mode === "activation"
          ? {items: activationItems, shown: 1, total: 1, truncated: 0, coverage: {eligible_pages: 8, typed_relation_pages: 3}}
          : {items: inboxItems, shown: 2, total: 5, truncated: 3, upstream_truncated: 1, note: "3 more not shown"};
      }
    } else if (name === "review_item_context") {
      const item = [...inboxItems, ...activationItems].find((candidate) => candidate.ref === body.ref) || inboxItems[0];
      data = contextFor(item);
    } else if (name === "connect_memory") {
      if (body.operation === "accept-relation") {
        const code = controls.decisionErrors?.shift();
        if (code) {
          await route.fulfill({status: 200, contentType: "application/json", body: JSON.stringify({success: false, error: {code, message: `Synthetic ${code} refusal.`}})});
          return;
        }
        if (controls.decisionBarrier) await controls.decisionBarrier.hold();
        controls.removedRefs.add(body.ref);
        data = {accepted: true};
      } else {
        data = {mutated: false, warnings: [], candidates: [{from: inboxItems[0].path, to: "Knowledge Base/Notes/Insights/related.md", relation_type: "supports", method: "recorded-link"}]};
      }
    } else if (name === "triage_memory" && body.ref?.startsWith("exomem://review/relation/")) {
      const code = controls.decisionErrors?.shift();
      if (code) {
        await route.fulfill({status: 200, contentType: "application/json", body: JSON.stringify({success: false, error: {code, message: `Synthetic ${code} refusal.`}})});
        return;
      }
      if (controls.decisionBarrier) await controls.decisionBarrier.hold();
      controls.removedRefs.add(body.ref);
      data = {state: body.action};
    } else {
      data = {state: body.action || "confirmed", path: inboxItems[0].path};
    }
    await route.fulfill({status: 200, contentType: "application/json", body: JSON.stringify({success: true, data})});
  });
}

async function connect(page, calls, controls = {}) {
  await mockApi(page, calls, controls);
  await page.goto("/studio/");
  await page.getByLabel("REST bearer key").fill("browser-session-key");
  await page.getByRole("button", {name: "Connect", exact: true}).click();
  await expect(page.getByRole("heading", {name: "Epistemic Inbox"})).toBeVisible();
}

async function connectRelations(page, calls, controls = {}) {
  await connect(page, calls, controls);
  const relations = page.getByRole("tab", {name: "Relations"});
  await relations.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", {name: "Relation Queue"})).toBeVisible();
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

test("relation queue preserves order, bounded truth, and complete governed payloads", async ({page}) => {
  const calls = [];
  const controls = {};
  const consoleErrors = [];
  const failedRequests = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("requestfailed", (request) => failedRequests.push(request.url()));
  await connectRelations(page, calls, controls);

  await expect(page.locator(".relation-group h3")).toHaveText(["Z source", "A source"]);
  await expect(page.locator(".relation-summary strong")).toHaveText([
    "supports → Knowledge Base/Notes/Insights/target-two",
    "part_of → Knowledge Base/Notes/Insights/target-one",
    "applies_to → Knowledge Base/Notes/Insights/target-a",
  ]);
  await expect(page.getByText("source: Knowledge Base/Notes/Insights/z-source.md").first()).toBeVisible();
  await expect(page.getByText("eligible pages")).toBeVisible();
  await expect(page.locator("#worklist-status")).toContainText("not the complete vault backlog");
  expect(calls.some((call) => call.name === "connect_memory" && call.body.operation === "suggest-relations")).toBeFalsy();

  const accept = page.getByRole("button", {name: /Accept supports.*target-two/});
  await accept.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByLabel("Audit reason")).toBeFocused();
  await page.getByLabel("Audit reason").fill("Accepted after bounded Studio review");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", {name: "Confirm governed accept"})).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(accept).toHaveCount(0);
  await expect(page.getByRole("button", {name: /Accept part_of.*target-one/})).toBeFocused();
  const acceptedCall = calls.find((call) => call.name === "connect_memory" && call.body.operation === "accept-relation");
  expect(acceptedCall.body).toEqual({
    operation: "accept-relation",
    ref: "exomem://review/relation/z-second",
    path: "Knowledge Base/Notes/Insights/z-source.md",
    expected_hash: "hash-z",
    why: "Accepted after bounded Studio review",
    expected_fingerprint: "fp-z-second",
  });

  const dismiss = page.getByRole("button", {name: /Dismiss part_of.*target-one/});
  await dismiss.focus();
  await page.keyboard.press("Enter");
  await page.getByLabel("Audit reason").fill("Dismissed after bounded Studio review");
  await page.getByRole("button", {name: "Confirm dismiss"}).focus();
  await page.keyboard.press("Enter");
  await expect(dismiss).toHaveCount(0);
  await expect(page.getByRole("button", {name: /Accept applies_to.*target-a/})).toBeFocused();
  const dismissedCall = calls.find((call) => call.name === "triage_memory" && call.body.action === "dismiss" && call.body.ref.includes("z-first"));
  expect(dismissedCall.body).toEqual({
    ref: "exomem://review/relation/z-first",
    source_path: "Knowledge Base/Notes/Insights/z-source.md",
    action: "dismiss",
    until: null,
    why: "Dismissed after bounded Studio review",
    expected_fingerprint: "fp-z-first",
  });

  const snooze = page.getByRole("button", {name: /Snooze applies_to.*target-a/});
  await snooze.focus();
  await page.keyboard.press("Enter");
  await page.getByLabel("Snooze through").fill("2026-09-30");
  await page.getByLabel("Audit reason").fill("Snoozed for bounded follow-up");
  await page.getByRole("button", {name: "Confirm snooze"}).focus();
  await page.keyboard.press("Enter");
  await expect(snooze).toHaveCount(0);
  await expect(page.getByRole("button", {name: "Refresh worklist"})).toBeFocused();
  const snoozedCall = calls.find((call) => call.name === "triage_memory" && call.body.action === "snooze");
  expect(snoozedCall.body).toEqual({
    ref: "exomem://review/relation/a-only",
    source_path: "Knowledge Base/Notes/Insights/a-source.md",
    action: "snooze",
    until: "2026-09-30",
    why: "Snoozed for bounded follow-up",
    expected_fingerprint: "fp-a-only",
  });
  expect(controls.relationQueueCalls).toBe(4);
  expect(consoleErrors).toEqual([]);
  expect(failedRequests).toEqual([]);
});

test("relation queue waits for the governed response before refreshing", async ({page}) => {
  const calls = [];
  const decisionBarrier = governedResponseBarrier();
  const controls = {decisionBarrier};
  const consoleErrors = [];
  const failedRequests = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("requestfailed", (request) => failedRequests.push(request.url()));
  await connectRelations(page, calls, controls);

  const accept = page.getByRole("button", {name: /Accept supports.*target-two/});
  await accept.focus();
  await page.keyboard.press("Enter");
  await page.getByLabel("Audit reason").fill("Accepted after delayed governed response");
  const confirm = page.getByRole("button", {name: "Confirm governed accept"});
  await confirm.focus();
  await page.keyboard.press("Enter");
  await decisionBarrier.arrived;

  expect(controls.relationQueueCalls).toBe(1);
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(confirm).toBeDisabled();
  await expect(accept).toHaveCount(1);

  decisionBarrier.release();
  await expect.poll(() => controls.relationQueueCalls).toBe(2);
  await expect(accept).toHaveCount(0);
  await expect(page.getByRole("dialog")).toBeHidden();
  await expect(page.getByRole("button", {name: /Accept part_of.*target-one/})).toBeFocused();
  expect(controls.relationQueueCalls).toBe(2);
  expect(consoleErrors).toEqual([]);
  expect(failedRequests).toEqual([]);
});

for (const driftCode of ["REVIEW_ITEM_CHANGED", "REVIEW_REFRESH_REQUIRED", "STALE_EDIT"]) {
  test(`relation ${driftCode} refusal preserves the candidate until explicit refresh`, async ({page}) => {
    const calls = [];
    const controls = {decisionErrors: [driftCode]};
    const consoleErrors = [];
    const failedRequests = [];
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    page.on("requestfailed", (request) => failedRequests.push(request.url()));
    await connectRelations(page, calls, controls);
    const accept = page.getByRole("button", {name: /Accept supports.*target-two/});
    await accept.focus();
    await page.keyboard.press("Enter");
    await page.getByRole("button", {name: "Confirm governed accept"}).focus();
    const queueCallsBefore = controls.relationQueueCalls;
    await page.keyboard.press("Enter");

    await expect(page.getByRole("alert")).toContainText("refresh required");
    await expect(page.getByRole("alert")).toBeFocused();
    await expect(accept).toHaveCount(1);
    expect(controls.relationQueueCalls).toBe(queueCallsBefore);

    await page.keyboard.press("Escape");
    await expect(accept).toBeFocused();
    await page.getByRole("button", {name: "Refresh worklist"}).focus();
    await page.keyboard.press("Enter");
    await expect.poll(() => controls.relationQueueCalls).toBe(queueCallsBefore + 1);
    await expect(page.getByRole("button", {name: "Refresh worklist"})).toBeFocused();
    expect(controls.relationQueueCalls).toBe(2);
    expect(consoleErrors).toEqual([]);
    expect(failedRequests).toEqual([]);
  });
}

test("narrow relation queue distinguishes retryable states and refreshes to a truncated bounded view", async ({page}) => {
  const calls = [];
  const controls = {queueResponses: [
    relationQueue({status: "warming"}),
    relationQueue({status: "pending"}),
    relationQueue({status: "unavailable"}),
    relationQueue(),
  ]};
  const consoleErrors = [];
  const failedRequests = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("requestfailed", (request) => failedRequests.push(request.url()));
  await page.setViewportSize({width: 390, height: 844});
  await connectRelations(page, calls, controls);
  await expect(page.locator("#worklist-status")).toContainText("warming");
  await expect(page.locator("#worklist-status")).toContainText("Retry");
  await expect(page.getByText("No relation candidates await review.")).toHaveCount(0);

  const refresh = page.getByRole("button", {name: "Refresh worklist"});
  await refresh.focus();
  await page.keyboard.press("Enter");
  await expect(refresh).toBeFocused();
  await expect(page.locator("#worklist-status")).toContainText("pending");
  await expect(page.locator("#worklist-status")).not.toContainText("warming");
  await page.keyboard.press("Enter");
  await expect(page.locator("#worklist-status")).toContainText("unavailable");
  await expect(page.locator("#worklist-status")).not.toContainText("pending");
  await page.keyboard.press("Enter");
  await expect(page.locator("#worklist-status")).toContainText("not the complete vault backlog");
  await expect(page.getByRole("button", {name: /Accept supports.*target-two/})).toBeVisible();
  expect(calls.some((call) => call.name === "connect_memory" && call.body.operation === "suggest-relations")).toBeFalsy();
  expect(controls.relationQueueCalls).toBe(4);
  expect(consoleErrors).toEqual([]);
  expect(failedRequests).toEqual([]);
});
