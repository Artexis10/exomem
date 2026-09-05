export function visibleItems(report, category = "") {
  const items = Array.isArray(report?.items) ? report.items : [];
  if (!category) return items;
  return items.filter((item) => (item.categories || []).includes(category));
}

export function categoriesFor(report) {
  const categories = new Set();
  for (const item of report?.items || []) {
    for (const category of item.categories || []) categories.add(category);
  }
  return [...categories].sort();
}

export function reportStatus(report, visibleCount) {
  const total = Number(report?.total || 0);
  const truncated = Number(report?.truncated || 0);
  const upstream = Number(report?.upstream_truncated || 0);
  const parts = [`${visibleCount} shown`, `${total} in this server view`];
  if (truncated) parts.push(`${truncated} omitted by the requested limit`);
  if (upstream) parts.push(`${upstream} capped upstream`);
  return parts.join(" · ");
}

export function worklistFiltersVisible(mode) {
  return mode !== "relation-queue";
}

export function relationQueueModel(response) {
  const queue = response && typeof response === "object" ? response : {};
  const groups = Array.isArray(queue.groups) ? queue.groups : [];
  const serverStatus = String(queue.status || "available");
  let state;
  if (["warming", "pending", "unavailable"].includes(serverStatus)) state = serverStatus;
  else state = groups.length ? "available" : "empty";
  const itemCount = groups.reduce(
    (total, group) => total + (Array.isArray(group?.items) ? group.items.length : 0),
    0,
  );
  return {
    state,
    groups,
    shown: Number(queue.shown ?? itemCount),
    pagesShown: Number(queue.pages_shown ?? groups.length),
    pagesTruncated: Number(queue.pages_truncated || 0),
    coverage: queue.coverage && typeof queue.coverage === "object" ? queue.coverage : null,
    retryable: Boolean(queue.retryable),
    retryAfter: queue.retry_after || null,
    nextAction: queue.next_action || null,
  };
}

export function relationQueueStatus(model) {
  if (model.state === "warming") {
    return "Relation queue warming: the graph-backed review view is not current yet. Retry with Refresh worklist.";
  }
  if (model.state === "pending") {
    return "Relation queue pending graph recovery. Retry with Refresh worklist after recovery advances.";
  }
  if (model.state === "unavailable") {
    return "Relation queue unavailable: graph-backed review cannot run right now. Retry with Refresh worklist.";
  }
  if (model.state === "empty") {
    return "No relation candidates await review in this bounded server view.";
  }
  const candidates = `${model.shown} candidate${model.shown === 1 ? "" : "s"}`;
  const pages = `${model.pagesShown} page${model.pagesShown === 1 ? "" : "s"}`;
  let status = `${candidates} across ${pages} in this bounded server view.`;
  if (model.pagesTruncated) {
    status += ` ${model.pagesTruncated} additional page${model.pagesTruncated === 1 ? " was" : "s were"} omitted; this is not the complete vault backlog.`;
  }
  return status;
}

export function sectionState(section) {
  if (!section || (section.available === false && section.reason)) return "unavailable";
  if (section.truncated || Number(section.omitted || 0) > 0) return "truncated";
  const records = section.items || section.pages || section.nodes || section.entries || section.versions;
  if (Array.isArray(records) && records.length === 0) return "empty";
  return "available";
}
