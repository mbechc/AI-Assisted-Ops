// Review queue filters: what can be filtered, and how filters map to the URL and to the API.
// The URL is the source of truth, so filtered views can be bookmarked, shared and saved.

export type Attr = "source" | "task" | "status" | "group" | "target" | "confidence" | "blocked";
export type ValueKind = "multi" | "text" | "number" | "yesno";

export interface Filter {
  attr: Attr;
  op: string;
  values: string[];
}

interface OpDef {
  op: string;
  label: string;
  urlKey: string;
  apiKey: string;
  kind: ValueKind;
}

export const ATTRS: { attr: Attr; label: string; ops: OpDef[] }[] = [
  { attr: "source", label: "Catalyst Center", ops: [
    { op: "is", label: "is", urlKey: "source", apiKey: "source_id", kind: "multi" },
    { op: "not", label: "is not", urlKey: "source.not", apiKey: "source_id_not", kind: "multi" },
  ] },
  { attr: "task", label: "Task", ops: [
    { op: "is", label: "is", urlKey: "task", apiKey: "task_key", kind: "multi" },
    { op: "not", label: "is not", urlKey: "task.not", apiKey: "task_key_not", kind: "multi" },
  ] },
  { attr: "status", label: "Status", ops: [
    { op: "is", label: "is", urlKey: "status", apiKey: "status", kind: "multi" },
    { op: "not", label: "is not", urlKey: "status.not", apiKey: "status_not", kind: "multi" },
  ] },
  { attr: "group", label: "Group / site", ops: [
    { op: "is", label: "is", urlKey: "group", apiKey: "group", kind: "multi" },
    { op: "not", label: "is not", urlKey: "group.not", apiKey: "group_not", kind: "multi" },
    { op: "has", label: "contains", urlKey: "group.has", apiKey: "group_contains", kind: "text" },
  ] },
  { attr: "target", label: "Target name", ops: [
    { op: "has", label: "contains", urlKey: "target.has", apiKey: "target_contains", kind: "text" },
  ] },
  { attr: "confidence", label: "Confidence", ops: [
    { op: "min", label: "at least", urlKey: "conf.min", apiKey: "min_confidence", kind: "number" },
    { op: "max", label: "at most", urlKey: "conf.max", apiKey: "max_confidence", kind: "number" },
  ] },
  { attr: "blocked", label: "Needs manual choice", ops: [
    { op: "is", label: "is", urlKey: "blocked", apiKey: "blocked", kind: "yesno" },
  ] },
];

export const STATUS_LABELS: Record<string, string> = {
  proposed: "Awaiting review", reported: "Reported (L0)", approved: "Approved", executing: "Executing",
  done: "Done", failed: "Failed", rejected: "Rejected", superseded: "Superseded",
};

const DEFAULT: Filter[] = [{ attr: "status", op: "is", values: ["proposed"] }];
const ALL_KEY = "all";

export function opDef(attr: Attr, op: string): OpDef {
  return ATTRS.find((a) => a.attr === attr)!.ops.find((o) => o.op === op)!;
}

export function attrLabel(attr: Attr): string {
  return ATTRS.find((a) => a.attr === attr)!.label;
}

const isMulti = (kind: ValueKind) => kind === "multi";

/** Filters from the URL; a URL without any filter shows what awaits review. */
export function parseFilters(sp: URLSearchParams): Filter[] {
  const out: Filter[] = [];
  for (const a of ATTRS) {
    for (const o of a.ops) {
      const raw = sp.getAll(o.urlKey).flatMap((v) => (a.attr === "status" ? v.split(",") : [v])).filter(Boolean);
      if (!raw.length) continue;
      out.push({ attr: a.attr, op: o.op, values: isMulti(o.kind) ? [...new Set(raw)] : [raw[raw.length - 1]] });
    }
  }
  if (!out.length && !sp.has(ALL_KEY) && !sp.get("q")) return DEFAULT;
  return out;
}

/** URL query for filters plus the free-text search. */
export function filtersToQuery(filters: Filter[], q: string): string {
  const p = new URLSearchParams();
  filters.forEach((f) => f.values.forEach((v) => p.append(opDef(f.attr, f.op).urlKey, v)));
  if (q) p.set("q", q);
  if (!filters.length && !q) p.set(ALL_KEY, "");
  return p.toString();
}

/** Parameters for GET /items, /items/groups and /review/facets. */
export function filtersToApi(filters: Filter[], q: string): Record<string, string | string[]> {
  const out: Record<string, string | string[]> = {};
  filters.forEach((f) => {
    const d = opDef(f.attr, f.op);
    if (isMulti(d.kind)) out[d.apiKey] = [...((out[d.apiKey] as string[]) || []), ...f.values];
    else if (d.kind === "yesno") out[d.apiKey] = f.values[0] === "yes" ? "true" : "false";
    else out[d.apiKey] = f.values[0];
  });
  if (q) out.q = q;
  return out;
}

/** Body filter for POST /items/decide (the server limits bulk decisions to items awaiting review). */
export function filtersToDecide(filters: Filter[], q: string): Record<string, unknown> {
  const api = filtersToApi(filters, q);
  const body: Record<string, unknown> = {};
  Object.entries(api).forEach(([k, v]) => {
    if (k === "source_id" || k === "source_id_not") body[k] = (v as string[]).map(Number);
    else if (k === "min_confidence" || k === "max_confidence") body[k] = Number(v);
    else if (k === "blocked") body[k] = v === "true";
    else body[k] = v;
  });
  return body;
}

/** Adds a filter, replacing one with the same attribute and operator. */
export function upsert(filters: Filter[], f: Filter, replaceIndex?: number): Filter[] {
  const rest = filters.filter((x, i) => i !== replaceIndex && !(x.attr === f.attr && x.op === f.op));
  const at = replaceIndex !== undefined ? Math.min(replaceIndex, rest.length) : rest.length;
  return [...rest.slice(0, at), f, ...rest.slice(at)];
}
