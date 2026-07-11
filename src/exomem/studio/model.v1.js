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

export function sectionState(section) {
  if (!section || (section.available === false && section.reason)) return "unavailable";
  if (section.truncated || Number(section.omitted || 0) > 0) return "truncated";
  const records = section.items || section.pages || section.nodes || section.entries || section.versions;
  if (Array.isArray(records) && records.length === 0) return "empty";
  return "available";
}
