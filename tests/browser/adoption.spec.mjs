// Adoption Studio browser acceptance (C.6). Loads the real packaged shell and
// intercepts only `/api/*` with deterministic fixtures, exactly like
// studio.spec.mjs. Python route/integration tests (test_studio_adoption_flows)
// separately exercise the real engine; this file proves the guided journey.
//
// Landed-contract note: retry rides `apply` with {retry_failed: true,
// only_paths} echoing plan_id (not the earlier `scope:"failed"` draft), and the
// 120 s awaiting-proposals notice is encoded as the poll slowdown 5 s → 15 s
// (adoption-model.v1.js pollDelay), asserted here with a mocked clock.
import {expect, test} from "@playwright/test";

const INVENTORY = [
  {path: "projects/alpha.md", eligible: true, junk: false},
  {path: "projects/beta.txt", eligible: true, junk: false},
  {path: "projects/deep/gamma.md", eligible: true, junk: false},
  {path: "old notes/meeting.md", eligible: true, junk: false},
  {path: "photos/pic.png", eligible: false, junk: false},
  {path: "old notes/conflict copy.md", eligible: false, junk: true},
];

const PLAN = {
  plan_id: "plan-1",
  totals: {copy: 4, skip_unsupported: 1, skip_junk: 1},
  items: [
    {original_path: "projects/alpha.md", title: "Alpha plan"},
    {original_path: "projects/beta.txt", title: "Beta risks"},
    {original_path: "projects/deep/gamma.md", title: "Gamma"},
    {original_path: "old notes/meeting.md", title: "Meeting"},
  ],
};

const APPLIED_OUTCOMES = {
  "projects/alpha.md": {status: "applied", target_path: "in/alpha.md"},
  "projects/beta.txt": {status: "applied", target_path: "in/beta.md"},
  "projects/deep/gamma.md": {status: "applied", target_path: "in/gamma.md"},
  "old notes/meeting.md": {status: "applied", target_path: "in/meeting.md"},
};

const PARTIAL_OUTCOMES = {
  "projects/alpha.md": {status: "applied", target_path: "in/alpha.md"},
  "projects/beta.txt": {status: "applied", target_path: "in/beta.md"},
  "projects/deep/gamma.md": {status: "failed", code: "NOT_FOUND", reason: "missing"},
  "old notes/meeting.md": {status: "failed", code: "SOURCE_CHANGED", reason: "changed"},
};

const HANDOFF = {
  prompt_text: "Look at my newly imported notes and suggest titles and groups.",
  links: {claude: "claude://claude.ai/new?q=continue", codex: 'codex exec "continue adoption"'},
};

// Every adoption_studio action returns the FULL presented run document.
function makeRun(phase, extra = {}) {
  return {
    schema_version: 1,
    run_id: "adr-test-1",
    phase,
    inventory: INVENTORY,
    scan_summary: {totals: {files: 6, dirs: 3, markdown: 3, binary: 1}, junk_counts: {conflict: 1}},
    pack_suggestions: [{name: "Projects"}],
    selection: null,
    plan: null,
    outcomes: {},
    handoff: null,
    errors: [],
    ...extra,
  };
}

function proposalItem(id, title) {
  return {ref: `exomem://review/adoption/adr-test-1/${id}`, fingerprint: `fp-${id}`, title, state: "open"};
}

// handlers: {"<command>" | "adoption_studio:<action>": data | (body) => data}.
// Return {__error: {status, code, message}} to produce a REST error envelope.
async function mockApi(page, calls, handlers) {
  await page.route("**/api/*", async (route) => {
    const request = route.request();
    const name = new URL(request.url()).pathname.split("/").pop();
    const body = request.postDataJSON() || {};
    calls.push({name, body});
    const key = name === "adoption_studio" ? `adoption_studio:${body.action}` : name;
    const handler = handlers[key] ?? handlers[name];
    if (handler === undefined) {
      await route.fulfill({status: 500, contentType: "application/json",
        body: JSON.stringify({success: false, error: {code: "UNMOCKED", message: `No mock for ${key}`}})});
      return;
    }
    const result = typeof handler === "function" ? handler(body) : handler;
    if (result && result.__error) {
      const {status = 409, code, message} = result.__error;
      await route.fulfill({status, contentType: "application/json",
        body: JSON.stringify({success: false, error: {code, message}})});
      return;
    }
    await route.fulfill({status: 200, contentType: "application/json",
      body: JSON.stringify({success: true, data: result})});
  });
}

// The auth probe is review_memory(mode=attention); adoption proposals use
// mode=adoption. Both ride one handler so tests can tell them apart.
function emptyInbox(adoptionItems = () => []) {
  return (body) => body.mode === "adoption"
    ? {items: adoptionItems(), shown: 0, total: 0, truncated: 0}
    : {items: [], shown: 0, total: 0, truncated: 0};
}

async function connectAdopt(page, calls, handlers, {query = "?view=adopt"} = {}) {
  await mockApi(page, calls, handlers);
  await page.goto(`/studio/${query}`);
  await page.getByLabel("REST bearer key").fill("browser-session-key");
  await page.getByRole("button", {name: "Connect", exact: true}).click();
}

const guarantee = (page) => page.locator("#adopt-guarantee");
const applyCalls = (calls) =>
  calls.filter((c) => c.name === "adoption_studio" && c.body.action === "apply");

test("deterministic happy path with guarantee badge and write gated on the dialog", async ({page}) => {
  const calls = [];
  let run = makeRun("selecting");
  await connectAdopt(page, calls, {
    review_memory: emptyInbox(),
    "adoption_studio:start": () => run,
    "adoption_studio:status": () => run,
    "adoption_studio:select": () => ({...run, selected_count: 4, selectable_count: 4}),
    "adoption_studio:plan": () => (run = makeRun("planned", {plan: PLAN})),
    "adoption_studio:apply": () => (run = makeRun("applied", {
      plan: PLAN, outcomes: APPLIED_OUTCOMES, verified_unchanged: 4, verified_total: 4, handoff: HANDOFF,
    })),
    ask_memory: {hits: [{title: "Alpha plan", path: "in/alpha.md", excerpt: "Ship the alpha."}]},
  });

  // Step 1: start — the originals-untouched guarantee is visible from the door.
  await expect(page.getByRole("heading", {name: "Bring your notes and files into Exomem"})).toBeVisible();
  await expect(guarantee(page)).toContainText("Copies only");
  await page.getByRole("button", {name: "Look through my files"}).click();

  // Step 2: findings.
  await expect(page.getByRole("heading", {name: "Here's what we found"})).toBeVisible();
  await expect(page.locator("#adopt-tiles")).toContainText("6");
  await expect(page.getByText("Your folder looks like it includes: Projects")).toBeVisible();
  await expect(guarantee(page)).toBeVisible();
  await page.getByRole("button", {name: "Choose what to bring in"}).click();

  // Step 3: choose — folders default ON, junk default OFF.
  await expect(page.getByRole("heading", {name: "Choose what to bring in"})).toBeVisible();
  await expect(page.locator("#adopt-status")).toHaveText("4 of 4 text notes selected · junk skipped");
  await expect(guarantee(page)).toBeVisible();
  await page.getByRole("button", {name: "Show me exactly what will happen"}).click();

  // Step 4: preview — exact plan, and still zero write-class calls.
  await expect(page.locator("#adopt-contract")).toContainText("Exomem will COPY 4 files");
  await expect(page.getByText("0 files will be changed, moved, or deleted — always")).toBeVisible();
  await expect(guarantee(page)).toBeVisible();
  expect(applyCalls(calls)).toHaveLength(0);

  // Step 5: apply happens ONLY through the explicit dialog confirm.
  await page.getByRole("button", {name: "Bring these files in"}).click();
  await expect(page.getByRole("dialog")).toContainText("Copy 4 files into Exomem?");
  expect(applyCalls(calls)).toHaveLength(0);
  await page.getByRole("button", {name: "Yes, copy them in"}).click();
  await expect(page.getByRole("heading", {name: "All set — 4 files are in ✓"})).toBeVisible();
  expect(applyCalls(calls)).toHaveLength(1);
  expect(applyCalls(calls)[0].body.plan_id).toBe("plan-1");
  await expect(page.getByText("We double-checked your originals: 4 of 4 are byte-for-byte unchanged (checksums match).")).toBeVisible();
  await expect(guarantee(page)).toBeVisible();

  // Steps 6–7: skip organize, ask the first question.
  await page.getByRole("button", {name: "Skip to your first question"}).click();
  await expect(page.getByRole("heading", {name: "Ask your first question"})).toBeVisible();
  await expect(guarantee(page)).toBeVisible();
  await page.getByRole("button", {name: "Find my notes on Projects"}).click();
  await expect(page.locator("#adopt-answers")).toContainText("Alpha plan");

  // Deterministic-only: no handoff/proposal machinery was ever invoked.
  expect(calls.some((c) => c.name === "review_memory" && c.body.mode === "adoption")).toBeFalsy();
  expect(calls.some((c) => ["triage_memory", "review_item_context"].includes(c.name))).toBeFalsy();
  expect(calls.some((c) => c.name === "adoption_studio"
    && ["work-item", "propose", "apply-proposal", "finish"].includes(c.body.action))).toBeFalsy();
});

test("stale plan shows the honest banner and re-check preserves the selection", async ({page}) => {
  const calls = [];
  let planCallCount = 0;
  const run = makeRun("selecting");
  await connectAdopt(page, calls, {
    review_memory: emptyInbox(),
    "adoption_studio:start": () => run,
    "adoption_studio:status": () => run,
    "adoption_studio:select": (body) => ({...run, selected_count: body.exclude.includes("projects") ? 1 : 4, selectable_count: 4}),
    "adoption_studio:plan": () => {
      planCallCount += 1;
      return planCallCount === 1
        ? {__error: {status: 409, code: "ADOPTION_SOURCE_CHANGED", message: "2 files changed after the scan."}}
        : makeRun("planned", {plan: PLAN});
    },
  });
  await page.getByRole("button", {name: "Look through my files"}).click();
  await page.getByRole("button", {name: "Choose what to bring in"}).click();
  await page.locator("#adopt-folder-projects").uncheck();
  await expect(page.locator("#adopt-status")).toHaveText("1 of 4 text notes selected · junk skipped");
  await page.getByRole("button", {name: "Show me exactly what will happen"}).click();

  // The banner carries the server's own reason — no fabricated counts.
  await expect(page.locator("#adopt-stale-text")).toContainText("2 files changed after the scan.");
  await expect(page.locator("#adopt-stale-text")).toContainText("Nothing has been copied yet.");
  const select = calls.find((c) => c.name === "adoption_studio" && c.body.action === "select");
  expect(select.body.exclude).toEqual(["projects"]);
  // Untouched default-on roots ride include so the engine sees the same universe.
  expect(select.body.include).toEqual(["old notes"]);

  // Re-check lands back on choose with the explicit choices intact.
  await page.getByRole("button", {name: "Re-check my folder"}).click();
  await expect(page.getByRole("heading", {name: "Choose what to bring in"})).toBeVisible();
  await expect(page.locator("#adopt-folder-projects")).not.toBeChecked();
  await expect(page.locator("#adopt-status")).toHaveText("1 of 4 text notes selected · junk skipped");
  expect(applyCalls(calls)).toHaveLength(0);
});

test("partial apply groups failures in plain language and retries only the failed subset", async ({page}) => {
  const calls = [];
  let run = makeRun("selecting");
  await connectAdopt(page, calls, {
    review_memory: emptyInbox(),
    "adoption_studio:start": () => run,
    "adoption_studio:status": () => run,
    "adoption_studio:select": () => run,
    "adoption_studio:plan": () => (run = makeRun("planned", {plan: PLAN})),
    "adoption_studio:apply": (body) => (run = body.retry_failed
      ? makeRun("applied", {plan: PLAN, outcomes: APPLIED_OUTCOMES, verified_unchanged: 4, verified_total: 4})
      : makeRun("partial", {plan: PLAN, outcomes: PARTIAL_OUTCOMES, verified_unchanged: 4, verified_total: 4})),
  });
  await page.getByRole("button", {name: "Look through my files"}).click();
  await page.getByRole("button", {name: "Choose what to bring in"}).click();
  await page.getByRole("button", {name: "Show me exactly what will happen"}).click();
  await page.getByRole("button", {name: "Bring these files in"}).click();
  await page.getByRole("button", {name: "Yes, copy them in"}).click();

  await expect(page.getByRole("heading", {name: "2 files are in · 2 couldn't be copied"})).toBeVisible();
  await expect(page.locator("#adopt-failures")).toContainText("This file moved or was removed after the scan.");
  await expect(page.locator("#adopt-failures")).toContainText("This file changed after we looked, so we left it untouched.");

  await page.getByRole("button", {name: "Try those 2 again"}).click();
  await expect(page.getByRole("heading", {name: "All set — 4 files are in ✓"})).toBeVisible();
  const retry = applyCalls(calls)[1];
  expect(retry.body.retry_failed).toBe(true);
  expect(retry.body.only_paths).toEqual(["projects/deep/gamma.md", "old notes/meeting.md"]);
  expect(retry.body.plan_id).toBe("plan-1");
});

test("cancel dialog during apply keeps already-copied files and confirms explicitly", async ({page}) => {
  const calls = [];
  let run = makeRun("selecting");
  await connectAdopt(page, calls, {
    review_memory: emptyInbox(),
    "adoption_studio:start": () => run,
    "adoption_studio:status": () => run,
    "adoption_studio:select": () => run,
    "adoption_studio:plan": () => (run = makeRun("planned", {plan: PLAN})),
    "adoption_studio:apply": () => (run = makeRun("applying", {plan: PLAN})),
    "adoption_studio:cancel": (body) => {
      expect(body.why).toBe("user stopped apply");
      run = makeRun("cancelled", {plan: PLAN, outcomes: {"projects/alpha.md": {status: "applied", target_path: "in/alpha.md"}}});
      return run;
    },
  });
  await page.getByRole("button", {name: "Look through my files"}).click();
  await page.getByRole("button", {name: "Choose what to bring in"}).click();
  await page.getByRole("button", {name: "Show me exactly what will happen"}).click();
  await page.getByRole("button", {name: "Bring these files in"}).click();
  await page.getByRole("button", {name: "Yes, copy them in"}).click();

  await expect(page.getByRole("heading", {name: "Bringing your files in…"})).toBeVisible();
  await page.getByRole("button", {name: "Stop", exact: true}).click();
  await expect(page.getByRole("dialog")).toContainText("Stop bringing files in?");
  await page.getByRole("button", {name: "Stop now"}).click();
  await expect(page.getByText("Stopped. The 1 files already copied are safe; your originals are untouched either way.")).toBeVisible();
  expect(calls.filter((c) => c.name === "adoption_studio" && c.body.action === "cancel")).toHaveLength(1);
});

test.describe("handoff", () => {
  test.use({permissions: ["clipboard-read", "clipboard-write"]});

  test("copy button copies the prompt and proposal polling slows after 120 s", async ({page}) => {
    const calls = [];
    const run = makeRun("applied", {plan: PLAN, outcomes: APPLIED_OUTCOMES, verified_unchanged: 4, verified_total: 4, handoff: HANDOFF});
    await page.clock.install();
    await connectAdopt(page, calls, {
      review_memory: emptyInbox(),
      "adoption_studio:status": () => run,
    }, {query: "?view=adopt&run=adr-test-1"});

    await expect(page.getByRole("heading", {name: "All set — 4 files are in ✓"})).toBeVisible();
    await page.getByRole("button", {name: "Get help organizing (optional)"}).click();
    await expect(page.getByRole("heading", {name: "Want help organizing? (optional)"})).toBeVisible();
    await expect(page.locator("#adopt-prompt")).toHaveValue(HANDOFF.prompt_text);
    await expect(page.getByRole("link", {name: "Continue in Claude"})).toHaveAttribute("href", HANDOFF.links.claude);
    await expect(page.locator("#adopt-links code")).toHaveText(HANDOFF.links.codex);

    await page.getByRole("button", {name: "Copy prompt"}).click();
    await expect(page.locator("#adopt-status")).toHaveText("Copied ✓");
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(HANDOFF.prompt_text);

    // Waiting for suggestions polls review_memory(mode=adoption) every 5 s,
    // then every 15 s once 120 s have elapsed (the timeout slowdown).
    const adoptionPolls = () => calls.filter((c) => c.name === "review_memory" && c.body.mode === "adoption").length;
    await page.getByRole("button", {name: "I've sent it — wait for suggestions"}).click();
    await expect(page.getByText("No suggestions to review.")).toBeVisible();
    await expect.poll(adoptionPolls).toBe(1);
    for (let tick = 2; tick <= 4; tick += 1) {
      await page.clock.fastForward(5_100);
      await expect.poll(adoptionPolls).toBe(tick);
    }
    await page.clock.fastForward(110_000);
    await expect.poll(adoptionPolls).toBe(5);
    await page.clock.fastForward(5_100);
    await page.waitForTimeout(200);
    expect(adoptionPolls()).toBe(5);
    await page.clock.fastForward(10_000);
    await expect.poll(adoptionPolls).toBe(6);
  });
});

test("suggestions approve and reject ride governed verbs, and drift refreshes honestly", async ({page}) => {
  const calls = [];
  let open = [proposalItem("p1", "Group the project notes"), proposalItem("p2", "Link meeting to alpha")];
  let approveAttempts = 0;
  const run = makeRun("applied", {plan: PLAN, outcomes: APPLIED_OUTCOMES, handoff: HANDOFF});
  await connectAdopt(page, calls, {
    review_memory: emptyInbox(() => open),
    "adoption_studio:status": () => run,
    review_item_context: (body) => ({item: open.find((i) => i.ref === body.ref) || {}, target: {body: "Recorded proposal detail."}}),
    "adoption_studio:apply-proposal": (body) => {
      approveAttempts += 1;
      if (approveAttempts === 1) {
        return {__error: {status: 409, code: "REVIEW_ITEM_CHANGED", message: "fingerprint changed"}};
      }
      open = open.filter((i) => i.ref !== body.ref);
      return {state: "applied"};
    },
    triage_memory: (body) => {
      expect(body.action).toBe("dismiss");
      expect(body.expected_fingerprint).toBe("fp-p2");
      open = open.filter((i) => i.ref !== body.ref);
      return {state: "dismissed"};
    },
  }, {query: "?view=adopt&run=adr-test-1&astep=suggestions"});

  await expect(page.getByRole("heading", {name: "Review suggestions"})).toBeVisible();
  await page.getByRole("button", {name: "Group the project notes"}).click();
  await expect(page.locator("#adopt-proposal-detail")).toContainText("Recorded proposal detail.");

  // First approval drifts: the dialog reports it, nothing is applied, the list refreshes.
  await page.getByRole("button", {name: "Make this change", exact: true}).click();
  await page.getByRole("dialog").getByRole("button", {name: "Make this change"}).click();
  await expect(page.locator("#dialog-error")).toContainText("out of date");
  await page.getByRole("button", {name: "Cancel", exact: true}).click();

  // Second approval succeeds through adoption_studio(apply-proposal).
  await page.getByRole("button", {name: "Group the project notes"}).click();
  await page.getByRole("button", {name: "Make this change", exact: true}).click();
  await page.getByRole("dialog").getByRole("button", {name: "Make this change"}).click();
  await expect(page.getByRole("button", {name: "Group the project notes"})).toHaveCount(0);
  const approvals = calls.filter((c) => c.name === "adoption_studio" && c.body.action === "apply-proposal");
  expect(approvals).toHaveLength(2);
  expect(approvals[1].body.expected_fingerprint).toBe("fp-p1");
  expect(approvals[1].body.why).toBeTruthy();

  // Reject rides triage_memory(dismiss) and the list empties.
  await page.getByRole("button", {name: "Link meeting to alpha"}).click();
  await page.getByRole("button", {name: "No thanks"}).click();
  await expect(page.getByText("No suggestions to review.")).toBeVisible();
});

test("deep link to a finished run resumes straight onto the done screen", async ({page}) => {
  const calls = [];
  await connectAdopt(page, calls, {
    review_memory: emptyInbox(),
    "adoption_studio:status": () => makeRun("done", {plan: PLAN, outcomes: APPLIED_OUTCOMES}),
  }, {query: "?view=adopt&run=adr-test-1"});
  await expect(page.getByRole("heading", {name: "You're set up"})).toBeVisible();
  await expect(guarantee(page)).toBeVisible();
});

test("390×844 viewport with keyboard-only folder and file selection", async ({page}) => {
  const calls = [];
  let run = makeRun("selecting");
  await page.setViewportSize({width: 390, height: 844});
  await connectAdopt(page, calls, {
    review_memory: emptyInbox(),
    "adoption_studio:start": () => run,
    "adoption_studio:status": () => run,
    "adoption_studio:select": () => run,
    "adoption_studio:plan": () => (run = makeRun("planned", {plan: PLAN})),
  });
  await page.getByRole("button", {name: "Look through my files"}).click();
  await page.getByRole("button", {name: "Choose what to bring in"}).click();

  // Keyboard on the folder tree: Space toggles the focused folder rule. The
  // toggle re-renders the tree (new checkbox node), so a keyboard user
  // re-acquires focus (Tab) before toggling again — modeled by the re-focus.
  await page.locator("#adopt-folder-projects").focus();
  await page.keyboard.press("Space");
  await expect(page.locator("#adopt-status")).toHaveText("1 of 4 text notes selected · junk skipped");
  await page.locator("#adopt-folder-projects").focus();
  await page.keyboard.press("Space");
  await expect(page.locator("#adopt-status")).toHaveText("4 of 4 text notes selected · junk skipped");

  // Roving tabindex skips disabled (non-importable) boxes: arrowing from the
  // only enabled "old notes" file must not dead-end on the disabled junk row.
  await page.locator("#adopt-choose-tree li", {hasText: "old notes"}).getByRole("button", {name: "see files"}).click();
  const fileBoxes = page.locator("#adopt-files-panel input[type=checkbox]");
  await fileBoxes.first().focus();
  await page.keyboard.press("ArrowDown");
  await expect(fileBoxes.first()).toBeFocused();

  // Keyboard in a multi-file panel: arrows move, Space overrides one file.
  await page.locator("#adopt-choose-tree li", {hasText: "projects"}).getByRole("button", {name: "see files"}).click();
  await fileBoxes.first().focus();
  await page.keyboard.press("ArrowDown");
  await expect(fileBoxes.nth(1)).toBeFocused();
  await page.keyboard.press("ArrowUp");
  await expect(fileBoxes.first()).toBeFocused();
  await page.keyboard.press("Space");
  await expect(page.locator("#adopt-status")).toHaveText("3 of 4 text notes selected · junk skipped");

  // The preview step still fits and gates apply behind the dialog confirm.
  await page.getByRole("button", {name: "Show me exactly what will happen"}).click();
  await page.getByRole("button", {name: "Bring these files in"}).click();
  await expect(page.getByRole("dialog").getByRole("button", {name: "Yes, copy them in"})).toBeVisible();
  expect(applyCalls(calls)).toHaveLength(0);
});
