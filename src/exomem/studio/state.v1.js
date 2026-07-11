const MODES = new Set(["attention", "activation"]);
const STATES = new Set(["open", "snoozed", "dismissed", "all"]);
const PANELS = new Set(["context", "evolution"]);

export function readRoute() {
  const query = new URLSearchParams(window.location.search);
  return {
    mode: MODES.has(query.get("mode")) ? query.get("mode") : "attention",
    state: STATES.has(query.get("state")) ? query.get("state") : "open",
    category: query.get("category") || "",
    ref: query.get("ref") || "",
    panel: PANELS.has(query.get("panel")) ? query.get("panel") : "context",
  };
}

export function writeRoute(route, {replace = false} = {}) {
  const query = new URLSearchParams();
  if (route.mode !== "attention") query.set("mode", route.mode);
  if (route.state !== "open") query.set("state", route.state);
  if (route.category) query.set("category", route.category);
  if (route.ref) query.set("ref", route.ref);
  if (route.panel !== "context") query.set("panel", route.panel);
  const target = `${window.location.pathname}${query.size ? `?${query}` : ""}`;
  window.history[replace ? "replaceState" : "pushState"](route, "", target);
}

export function routePatch(current, patch) {
  return {...current, ...patch};
}
