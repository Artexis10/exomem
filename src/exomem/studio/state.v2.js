const MODES = new Set(["attention", "activation"]);
const STATES = new Set(["open", "snoozed", "dismissed", "all"]);
const PANELS = new Set(["context", "evolution"]);
const VIEWS = new Set(["review", "adopt"]);
const ASTEPS = new Set(["start", "findings", "choose", "preview", "organize", "suggestions", "question"]);

export function readRoute() {
  const query = new URLSearchParams(window.location.search);
  return {
    mode: MODES.has(query.get("mode")) ? query.get("mode") : "attention",
    state: STATES.has(query.get("state")) ? query.get("state") : "open",
    category: query.get("category") || "",
    ref: query.get("ref") || "",
    panel: PANELS.has(query.get("panel")) ? query.get("panel") : "context",
    view: VIEWS.has(query.get("view")) ? query.get("view") : "review",
    run: query.get("run") || "",
    astep: ASTEPS.has(query.get("astep")) ? query.get("astep") : "start",
  };
}

export function writeRoute(route, {replace = false} = {}) {
  const query = new URLSearchParams();
  // Review params first: legacy review URLs serialize byte-identically to v1.
  if (route.mode !== "attention") query.set("mode", route.mode);
  if (route.state !== "open") query.set("state", route.state);
  if (route.category) query.set("category", route.category);
  if (route.ref) query.set("ref", route.ref);
  if (route.panel !== "context") query.set("panel", route.panel);
  // Adoption params emit only when non-default, so review defaults stay clean.
  if (route.view !== "review") query.set("view", route.view);
  if (route.view === "adopt" && route.run) query.set("run", route.run);
  if (route.view === "adopt" && route.astep && route.astep !== "start") query.set("astep", route.astep);
  const target = `${window.location.pathname}${query.size ? `?${query}` : ""}`;
  window.history[replace ? "replaceState" : "pushState"](route, "", target);
}

export function routePatch(current, patch) {
  return {...current, ...patch};
}
