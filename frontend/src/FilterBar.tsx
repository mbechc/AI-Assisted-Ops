import { KeyboardEvent, ReactNode, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { useAsync } from "./components";
import {
  ATTRS, Attr, Filter, SCOPE, STATUS_LABELS, View, attrLabel, filtersToQuery, opDef, parseFilters, upsert,
} from "./reviewFilters";
import { Facets, SavedFilter, Task } from "./types";

export interface Option {
  value: string;
  label: string;
  count?: number;
}

function useOutside(ref: React.RefObject<HTMLElement | null>, onOutside: () => void, active: boolean) {
  useEffect(() => {
    if (!active) return;
    const h = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && onOutside();
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [active, onOutside, ref]);
}

/** Value lists (with counts) and display names for each attribute. */
export function filterOptions(facets: Facets | null, tasks: Task[], view: View = "inbox") {
  const taskName = (k: string) => tasks.find((t) => t.key === k)?.name || k;
  const count = (list: { value: string; count: number }[] | undefined, v: string) => list?.find((x) => x.value === v)?.count ?? 0;
  const options: Record<Attr, Option[]> = {
    source: (facets?.sources || []).filter((s) => s.id !== null)
      .map((s) => ({ value: String(s.id), label: s.name, count: s.count })),
    task: tasks.filter((t) => t.category !== "Collector")
      .map((t) => ({ value: t.key, label: t.name, count: count(facets?.tasks, t.key) }))
      .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label)),
    status: SCOPE[view].map((v) => ({ value: v, label: STATUS_LABELS[v] || v, count: count(facets?.statuses, v) })),
    group: (facets?.groups || []).map((g) => ({ value: g.value, label: g.value, count: g.count })),
    target: [],
    confidence: [],
    blocked: [{ value: "yes", label: "Yes" }, { value: "no", label: "No" }],
  };
  const label = (attr: Attr, v: string) => {
    if (attr === "source") return facets?.sources.find((s) => String(s.id) === v)?.name || `#${v}`;
    if (attr === "task") return taskName(v);
    if (attr === "status") return STATUS_LABELS[v] || v;
    if (attr === "blocked") return v === "yes" ? "Yes" : "No";
    return v;
  };
  return { options, label };
}

export default function FilterBar({ view, filters, q, total, facets, tasks, onChange }: {
  view: View; filters: Filter[]; q: string; total: number | undefined; facets: Facets | null; tasks: Task[];
  onChange: (filters: Filter[], q: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<number | undefined>(undefined);
  const [search, setSearch] = useState(q);
  const { options, label } = filterOptions(facets, tasks, view);

  useEffect(() => setSearch(q), [q]);
  useEffect(() => {
    if (search === q) return;
    const t = setTimeout(() => onChange(filters, search.trim()), 300);
    return () => clearTimeout(t);
  }, [search]);

  const apply = (f: Filter) => {
    onChange(upsert(filters, f, editing), q);
    setEditing(undefined);
  };

  return (
    <div className="filterbar">
      <div className="filter-toolbar">
        <input className="filter-search" type="search" placeholder="Search target or group" value={search}
          onChange={(e) => setSearch(e.target.value)} />
        <button className={`filter-toggle ${open ? "open" : ""}`} onClick={() => { setOpen(!open); setEditing(undefined); }}
          aria-expanded={open}>
          Filters{filters.length ? <span className="count">{filters.length}</span> : null}
          <span className="chev" aria-hidden>{open ? "\u25B4" : "\u25BE"}</span>
        </button>
        <span className="muted">{total === undefined ? "" : `${total.toLocaleString()} result${total === 1 ? "" : "s"}`}</span>
        <div className="spacer" />
        <SavedFilters current={filtersToQuery(filters, q)}
          onApply={(query) => {
            const sp = new URLSearchParams(query);
            onChange(parseFilters(sp, view), sp.get("q") || "");
          }} />
      </div>

      {(open || editing !== undefined) && (
        <FilterBuilder key={editing ?? "new"} initial={editing !== undefined ? filters[editing] : undefined}
          options={options} onApply={(f) => { apply(f); if (editing !== undefined) setOpen(false); }}
          onCancel={() => { setOpen(false); setEditing(undefined); }} />
      )}

      {(filters.length > 0 || q) && (
        <div className="filter-chips">
          {filters.map((f, i) => (
            <span key={`${f.attr}.${f.op}`} className={`chip ${editing === i ? "active" : ""}`}>
              <button className="chip-body" title="Edit filter" onClick={() => setEditing(i)}>
                <span className="chip-attr">{attrLabel(f.attr)}</span> {opDef(f.attr, f.op).label}{" "}
                <b>{f.values.map((v) => label(f.attr, v)).join(", ")}</b>
              </button>
              <button className="chip-x" aria-label="Remove filter"
                onClick={() => { onChange(filters.filter((_, n) => n !== i), q); setEditing(undefined); }}>
                {"\u00D7"}
              </button>
            </span>
          ))}
          {q && (
            <span className="chip">
              <span className="chip-body static"><span className="chip-attr">Search</span> <b>{q}</b></span>
              <button className="chip-x" aria-label="Clear search" onClick={() => onChange(filters, "")}>{"\u00D7"}</button>
            </span>
          )}
          <button className="link small" onClick={() => { onChange([], ""); setEditing(undefined); }}>Clear filters</button>
        </div>
      )}
    </div>
  );
}

function FilterBuilder({ initial, options, onApply, onCancel }: {
  initial?: Filter; options: Record<Attr, Option[]>; onApply: (f: Filter) => void; onCancel: () => void;
}) {
  const [attr, setAttr] = useState<Attr | "">(initial?.attr ?? "");
  const [op, setOp] = useState(initial?.op ?? "");
  const [values, setValues] = useState<string[]>(initial?.values ?? []);
  const ops = attr ? ATTRS.find((a) => a.attr === attr)!.ops : [];
  const kind = attr && op ? opDef(attr, op).kind : null;
  const valid = !!attr && !!op && values.length > 0 && values.every((v) => v.trim() !== "")
    && (kind !== "number" || values.every((v) => /^\d{1,3}$/.test(v) && Number(v) <= 100));

  const pickAttr = (a: Attr | "") => {
    setAttr(a);
    const first = a ? ATTRS.find((x) => x.attr === a)!.ops : [];
    setOp(first.length === 1 ? first[0].op : first[0]?.op ?? "");
    setValues([]);
  };
  const pickOp = (o: string) => {
    if (attr && opDef(attr, o).kind !== kind) setValues([]);
    setOp(o);
  };
  const submit = () => valid && attr && onApply({ attr, op, values: values.map((v) => v.trim()) });
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Enter") submit();
    if (e.key === "Escape") onCancel();
  };

  return (
    <div className="filter-panel" onKeyDown={onKey}>
      <span className="filter-by">Filter by:</span>
      <Labeled label="Attribute">
        <select autoFocus value={attr} onChange={(e) => pickAttr(e.target.value as Attr | "")}>
          <option value="">Select attribute</option>
          {ATTRS.map((a) => <option key={a.attr} value={a.attr}>{a.label}</option>)}
        </select>
      </Labeled>
      <Labeled label="Operator">
        <select value={op} disabled={!attr} onChange={(e) => pickOp(e.target.value)}>
          {!attr && <option value="">Select operator</option>}
          {ops.map((o) => <option key={o.op} value={o.op}>{o.label}</option>)}
        </select>
      </Labeled>
      <Labeled label="Value" wide>
        {!kind && <select disabled><option>Select value</option></select>}
        {kind === "multi" && attr && <MultiSelect options={options[attr]} values={values} onChange={setValues} />}
        {kind === "text" && <input value={values[0] ?? ""} placeholder="Text" onChange={(e) => setValues([e.target.value])} />}
        {kind === "number" && (
          <input type="number" min={0} max={100} value={values[0] ?? ""} placeholder="0-100"
            onChange={(e) => setValues(e.target.value === "" ? [] : [e.target.value])} />
        )}
        {kind === "yesno" && (
          <select value={values[0] ?? ""} onChange={(e) => setValues(e.target.value ? [e.target.value] : [])}>
            <option value="">Select value</option>
            {options.blocked.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        )}
      </Labeled>
      <div className="filter-actions">
        <button className="primary" disabled={!valid} onClick={submit}>{initial ? "Update filter" : "Apply filter"}</button>
        <button className="link" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

function Labeled({ label, wide, children }: { label: string; wide?: boolean; children: ReactNode }) {
  return (
    <label className={`filter-field ${wide ? "wide" : ""}`}>
      <span>{label}</span>
      {children}
    </label>
  );
}

function MultiSelect({ options, values, onChange }: { options: Option[]; values: string[]; onChange: (v: string[]) => void }) {
  const [open, setOpen] = useState(false);
  const [find, setFind] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  useOutside(ref, () => setOpen(false), open);
  const shown = options.filter((o) => o.label.toLowerCase().includes(find.toLowerCase()));
  const toggle = (v: string) => onChange(values.includes(v) ? values.filter((x) => x !== v) : [...values, v]);
  const summary = !values.length ? "Select value"
    : values.length <= 2 ? values.map((v) => options.find((o) => o.value === v)?.label || v).join(", ")
    : `${values.length} selected`;
  return (
    <div className="ms" ref={ref}>
      <button type="button" className={`ms-button ${values.length ? "" : "placeholder"}`} onClick={() => setOpen(!open)}
        aria-expanded={open}>
        <span>{summary}</span><span className="chev" aria-hidden>{open ? "\u25B4" : "\u25BE"}</span>
      </button>
      {open && (
        <div className="dropdown-menu ms-menu">
          {options.length > 8 && <input autoFocus placeholder="Find" value={find} onChange={(e) => setFind(e.target.value)} />}
          <div className="ms-list">
            {shown.map((o) => (
              <label key={o.value} className={`ms-option ${o.count === 0 ? "empty" : ""}`}>
                <input type="checkbox" checked={values.includes(o.value)} onChange={() => toggle(o.value)} />
                <span className="ms-label">{o.label}</span>
                {o.count !== undefined && <span className="ms-count">{o.count.toLocaleString()}</span>}
              </label>
            ))}
            {!shown.length && <div className="muted small ms-empty">No values</div>}
          </div>
          {values.length > 0 && <button className="link small" onClick={() => onChange([])}>Clear selection</button>}
        </div>
      )}
    </div>
  );
}

function SavedFilters({ current, onApply }: { current: string; onApply: (query: string) => void }) {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [name, setName] = useState("");
  const [shared, setShared] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  const list = useAsync(() => api.get<SavedFilter[]>("/review/saved-filters"), []);
  useOutside(ref, () => { setOpen(false); setSaving(false); }, open);
  const active = list.data?.find((f) => f.query === current);

  const save = async () => {
    setErr(null);
    try {
      await api.post("/review/saved-filters", { name: name.trim(), query: current, shared });
      setSaving(false);
      setName("");
      list.reload();
    } catch (e: any) {
      setErr(e.message);
    }
  };
  const remove = async (f: SavedFilter) => {
    if (!confirm(`Delete the saved filter "${f.name}"${f.shared ? " for everyone" : ""}?`)) return;
    try {
      await api.del(`/review/saved-filters/${f.id}`);
      list.reload();
    } catch (e: any) {
      setErr(e.message);
    }
  };
  const mine = (list.data || []).filter((f) => f.mine);
  const others = (list.data || []).filter((f) => !f.mine);

  const row = (f: SavedFilter) => (
    <div key={f.id} className={`saved-row ${active?.id === f.id ? "active" : ""}`}>
      <button className="saved-name" onClick={() => { onApply(f.query); setOpen(false); }}>
        {f.name}
        {f.shared && <span className="badge muted">{f.mine ? "shared" : f.owner}</span>}
      </button>
      {f.can_delete && <button className="chip-x" aria-label={`Delete ${f.name}`} onClick={() => remove(f)}>{"\u00D7"}</button>}
    </div>
  );

  return (
    <div className="dropdown" ref={ref}>
      <button onClick={() => setOpen(!open)} aria-expanded={open}>
        {active ? active.name : "Saved filters"} <span className="chev" aria-hidden>{open ? "\u25B4" : "\u25BE"}</span>
      </button>
      {open && (
        <div className="dropdown-menu right saved-menu">
          {err && <p className="err small">{err}</p>}
          {!saving ? (
            <>
              {mine.length > 0 && <div className="menu-head">My filters</div>}
              {mine.map(row)}
              {others.length > 0 && <div className="menu-head">Shared by others</div>}
              {others.map(row)}
              {!list.data?.length && <p className="muted small">No saved filters yet.</p>}
              <div className="menu-foot">
                <button className="link" disabled={!!active} onClick={() => setSaving(true)}>Save current filters...</button>
              </div>
            </>
          ) : (
            <div className="save-form">
              <input autoFocus placeholder="Name, e.g. CatC A hostname resets" maxLength={80} value={name}
                onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && name.trim() && save()} />
              <label className="row small"><input type="checkbox" checked={shared} onChange={(e) => setShared(e.target.checked)} /> Share with everyone</label>
              <p className="muted small">Saving with an existing name of yours replaces it.</p>
              <div className="row end">
                <button className="link" onClick={() => setSaving(false)}>Cancel</button>
                <button className="primary" disabled={!name.trim()} onClick={save}>Save</button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
