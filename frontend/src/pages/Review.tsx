import React, { Fragment, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useAuth } from "../App";
import { api, fmtDate, qsMulti } from "../api";
import { Confidence, Drawer, ErrorLine, Pager, Status, useAsync } from "../components";
import FilterBar, { filterOptions } from "../FilterBar";
import { Filter, View, filtersToApi, filtersToDecide, filtersToQuery, parseFilters, upsert } from "../reviewFilters";
import { Facets, Group, Site, Task, WorkItem } from "../types";

const LIMIT = 100;

const HEAD: Record<View, { title: string; sub: string }> = {
  inbox: {
    title: "Inbox",
    sub: "What needs a human now: approve, override or reject what the Expert Tasks propose, and read open findings. Decisions train the confidence calibration.",
  },
  history: {
    title: "History",
    sub: "What has happened: items approved, executed, failed, rejected or superseded, by people and by automation. Newest first.",
  },
};

export default function Review({ view: scope }: { view: View }) {
  const [sp, setSp] = useSearchParams();
  const tasks = useAsync(() => api.get<Task[]>("/tasks"), []);
  const filters = useMemo(() => parseFilters(sp, scope), [sp, scope]);
  const q = sp.get("q") || "";
  const apiParams = useMemo(() => filtersToApi(filters, q, scope), [filters, q, scope]);
  const key = JSON.stringify(apiParams);
  const inbox = scope === "inbox";
  const [tab, setView] = useState<"groups" | "list">(inbox ? "groups" : "list");
  const view = inbox ? tab : "list";
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [openItem, setOpenItem] = useState<number | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => { setOffset(0); setSelected(new Set()); }, [key]);
  useEffect(() => setView(inbox ? "groups" : "list"), [inbox]);

  const facets = useAsync(() => api.get<Facets>(`/review/facets${qsMulti(apiParams)}`), [key], 20000);
  const groups = useAsync(
    () => (inbox ? api.get<{ groups: Group[] }>(`/items/groups${qsMulti(apiParams)}`) : Promise.resolve({ groups: [] })),
    [key], 20000);
  const items = useAsync(
    () => api.get<{ total: number; items: WorkItem[] }>(
      `/items${qsMulti({ ...apiParams, limit: LIMIT, offset, ...(inbox ? {} : { sort: "-updated" }) })}`),
    [key, offset], 20000);

  const setFilters = (f: Filter[], text: string) => setSp(new URLSearchParams(filtersToQuery(f, text)));
  const refresh = () => { groups.reload(); items.reload(); facets.reload(); };
  const { label } = filterOptions(facets.data, tasks.data || []);
  const sourceCount = (facets.data?.sources || []).filter((s) => s.id !== null).length;
  const taskName = (k: string) => label("task", k);
  const sourceName = (id: number | null) => (id === null ? "(deleted)" : label("source", String(id)));

  const decide = async (body: Record<string, unknown>, text: string) => {
    setErr(null);
    try {
      const r = await api.post<{ updated: number[]; errors: { id: number; error: string }[] }>("/items/decide", body);
      setMsg(`${text}: ${r.updated.length} item(s).` + (r.errors.length ? ` ${r.errors.length} skipped (e.g. ${r.errors[0].error}).` : ""));
      setSelected(new Set());
      refresh();
    } catch (e: any) {
      setErr(e.message);
    }
  };

  const openGroup = (g: Group) => {
    let f = upsert(filters, { attr: "task", op: "is", values: [g.task_key] });
    if (g.source_id !== null && sourceCount > 1) f = upsert(f, { attr: "source", op: "is", values: [String(g.source_id)] });
    if (g.group !== null) f = upsert(f, { attr: "group", op: "is", values: [g.group] });
    setFilters(f, q);
    setView("list");
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{HEAD[scope].title}</h1>
          <p className="sub">{HEAD[scope].sub}</p>
        </div>
      </div>
      <FilterBar view={scope} filters={filters} q={q} total={items.data?.total} facets={facets.data} tasks={tasks.data || []}
        onChange={setFilters} />
      {msg && <div className="banner info">{msg}</div>}
      <ErrorLine error={err || items.error || groups.error || facets.error} />
      {inbox && (
        <div className="tabs">
          <button className={view === "groups" ? "active" : ""} onClick={() => setView("groups")}>
            By group ({groups.data?.groups.length ?? 0})
          </button>
          <button className={view === "list" ? "active" : ""} onClick={() => setView("list")}>
            Items ({items.data?.total ?? 0})
          </button>
        </div>
      )}
      {view === "groups" ? (
        <GroupsView
          groups={groups.data?.groups || null} taskName={taskName} sourceName={sourceName} showSource={sourceCount > 1}
          onOpen={openGroup}
        />
      ) : (
        <div className="card">
          {inbox && <div className="row" style={{ marginBottom: 10 }}>
            <span className="muted small">
              {selected.size ? `${selected.size} selected` : "Select items awaiting review to approve or reject them together."}
            </span>
            <div className="spacer" />
            <BulkBar
              selected={selected}
              onApprove={() => decide({ decision: "approve", ids: [...selected] }, "Approved")}
              onReject={(note) => decide({ decision: "reject", ids: [...selected], note }, "Rejected")}
              onApproveFilter={() => decide({ decision: "approve", filter: filtersToDecide(filters, q) }, "Approved all matching")}
            />
          </div>}
          <ItemsTable
            items={items.data?.items || []} selected={selected} setSelected={setSelected} onOpen={setOpenItem}
            taskName={taskName} sourceName={sourceName} showSource={sourceCount > 1}
            showTask={(facets.data?.tasks.length ?? 0) > 1}
          />
          <Pager total={items.data?.total || 0} limit={LIMIT} offset={offset} onChange={setOffset} />
        </div>
      )}
      {openItem !== null && <ItemDrawer id={openItem} onClose={() => setOpenItem(null)} onDecided={(m) => { setMsg(m); setOpenItem(null); refresh(); }} />}
    </>
  );
}

function GroupsView({ groups, taskName, sourceName, showSource, onOpen }: {
  groups: Group[] | null; taskName: (k: string) => string; sourceName: (id: number | null) => string; showSource: boolean;
  onOpen: (g: Group) => void;
}) {
  if (!groups) return null;
  const showTask = new Set(groups.map((g) => g.task_key)).size > 1;
  return (
    <div className="card">
      <table>
        <thead>
          <tr>
            <th>Group</th>{showTask && <th>Expert Task</th>}{showSource && <th>Catalyst Center</th>}
            <th className="num">Items</th><th className="num">Avg conf.</th>
          </tr>
        </thead>
        <tbody>
          {groups.map((g) => (
            <tr key={`${g.task_key}|${g.source_id}|${g.group}`} className="click" onClick={() => onOpen(g)}>
              <td>{g.group ?? <span className="muted">none</span>}</td>
              {showTask && <td className="small">{taskName(g.task_key)}</td>}
              {showSource && <td className="small">{sourceName(g.source_id)}</td>}
              <td className="num">{g.count}</td>
              <td className="num"><Confidence value={Math.round(g.avg_confidence)} small /></td>
            </tr>
          ))}
          {!groups.length && <tr><td colSpan={5} className="muted">Nothing matches the filters.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

function BulkBar({ selected, onApprove, onReject, onApproveFilter }: {
  selected: Set<number>; onApprove: () => void; onReject: (note: string) => void; onApproveFilter: () => void;
}) {
  const { can } = useAuth();
  if (!can("operator")) return null;
  return (
    <>
      <button className="ok" disabled={!selected.size} onClick={onApprove}>Approve selected ({selected.size})</button>
      <button className="danger" disabled={!selected.size} onClick={() => {
        const note = prompt("Reason for rejection (used to improve the task):");
        if (note !== null) onReject(note);
      }}>Reject selected</button>
      <button onClick={() => confirm("Approve every item awaiting review that matches the current filters (items needing a manual choice excluded)?") && onApproveFilter()}>
        Approve all matching
      </button>
    </>
  );
}

function ItemsTable({ items, selected, setSelected, onOpen, taskName, sourceName, showSource, showTask }: {
  items: WorkItem[]; selected: Set<number>; setSelected: (s: Set<number>) => void; onOpen: (id: number) => void;
  taskName: (k: string) => string; sourceName: (id: number | null) => string; showSource: boolean; showTask: boolean;
}) {
  const { can } = useAuth();
  const selectable = can("operator") && items.some((i) => i.status === "proposed");
  const open = items.filter((i) => i.status === "proposed");
  const allSel = open.length > 0 && open.every((i) => selected.has(i.id));
  const toggle = (id: number) => {
    const s = new Set(selected);
    s.has(id) ? s.delete(id) : s.add(id);
    setSelected(s);
  };
  const cols = 6 + (selectable ? 1 : 0) + (showSource ? 1 : 0) + (showTask ? 1 : 0);
  return (
    <table>
      <thead>
        <tr>
          {selectable && <th><input type="checkbox" checked={allSel} onChange={() => setSelected(allSel ? new Set() : new Set(open.map((i) => i.id)))} /></th>}
          <th>Target</th>{showTask && <th>Expert Task</th>}{showSource && <th>Catalyst Center</th>}
          <th>Proposal</th><th>Confidence</th><th>Status</th><th>Notes</th><th>Updated</th>
        </tr>
      </thead>
      <tbody>
        {items.map((i) => (
          <tr key={i.id} className={`click ${selected.has(i.id) ? "selected" : ""}`} onClick={() => onOpen(i.id)}>
            {selectable && (
              <td onClick={(e) => e.stopPropagation()}>
                {i.status === "proposed" && <input type="checkbox" checked={selected.has(i.id)} onChange={() => toggle(i.id)} />}
              </td>
            )}
            <td><b>{i.target_label}</b><div className="small muted">{i.group_label}</div></td>
            {showTask && <td className="small">{taskName(i.task_key)}</td>}
            {showSource && <td className="small">{sourceName(i.source_id ?? null)}</td>}
            <td className="small">{i.proposed || <span className="muted">-</span>}</td>
            <td><Confidence value={i.confidence} />{i.llm_status === "ok" && <span className="small muted" title={`Reviewed by ${i.llm_provider === "cloud" ? "Azure OpenAI" : "the local LLM"}`}> AI</span>}</td>
            <td><Status value={i.status} />{i.decided_by === "auto" && <div className="small muted">auto</div>}</td>
            <td className="small">
              {i.blocked_reason ? <span className="badge warn">needs choice</span> : i.concerns?.length ? <span className="muted">{i.concerns.length} concern(s)</span> : ""}
            </td>
            <td className="small muted">{fmtDate(i.updated_at)}</td>
          </tr>
        ))}
        {!items.length && <tr><td colSpan={cols} className="muted">No items match the filters.</td></tr>}
      </tbody>
    </table>
  );
}

function PayloadDetails({ item: i }: { item: WorkItem }) {
  const p = i.payload || {};
  const o = i.override_payload || {};
  const rows: [string, React.ReactNode][] = [["Action", <code>{i.action_type}</code>]];
  switch (i.action_type) {
    case "assign_site":
      rows.push(["Proposed site", p.site_hierarchy || <span className="muted">none</span>]);
      if (o.site_hierarchy) rows.push(["Operator override", o.site_hierarchy]);
      rows.push(["Device", `${p.hostname} / ${p.mgmt_ip} / ${p.platform}`], ["WLC", p.wlc_ip || "-"]);
      break;
    case "reassign_site":
      rows.push(["Current site", p.current_site], ["Proposed site", p.site_hierarchy || p.building || "-"],
        ["Mismatch", p.mismatch]);
      if (o.site_hierarchy) rows.push(["Operator override", o.site_hierarchy]);
      rows.push(["Device", `${p.hostname} / ${p.mgmt_ip} / ${p.family}`]);
      break;
    case "rename_ap":
      rows.push(["Current name", p.hostname], ["New name", <b>{o.hostname_new || p.hostname_new}</b>],
        ["Method", p.method === "history" ? "earlier name from inventory history"
          : p.method === "ai_template" ? "floor naming template learned by AI (validated)" : "floor naming convention"],
        ["Site", p.site || "-"], ["MAC", p.mac || "-"]);
      break;
    case "set_role":
      rows.push(["Current role", p.current_role], ["New role", <b>{o.role || p.role}</b>], ["Device", `${p.hostname} / ${p.platform}`],
        ["Site", p.site || "-"]);
      break;
    case "exclude_device":
    case "include_device":
      rows.push(["Device", `${p.hostname} (${p.family})`], ["Site", p.site || "-"]);
      if (p.reasons) rows.push(["Reasons", p.reasons.join("; ")], ["Window", `${p.days} days`]);
      if (p.mechanism) rows.push(["Excluded via", p.mechanism]);
      break;
    case "resync_device":
    case "report":
      Object.entries(p).filter(([k, v]) => v !== null && typeof v !== "object" && k !== "auto_eligible" && k !== "device_id")
        .forEach(([k, v]) => rows.push([k.replace(/_/g, " "), String(v)]));
      break;
    default:
      rows.push(["Payload", <pre>{JSON.stringify(p, null, 2)}</pre>]);
  }
  if (p.auto_eligible === false && i.action_type !== "report") rows.push(["Automation", <span className="badge warn">never auto-executed</span>]);
  return <div className="kv">{rows.map(([k, v], n) => <Fragment key={n}><div className="k">{k}</div><div>{v}</div></Fragment>)}</div>;
}

const LLM_STATUS_TEXT: Record<string, string> = {
  skipped: "not reviewed", unavailable: "AI unavailable", error: "AI error",
};
const LLM_STATUS_HELP: Record<string, string> = {
  skipped: "AI reviews only proposals of AI-capable tasks inside the task's confidence band, up to the per-run budget.",
  unavailable: "The AI provider could not be reached during this run; the rule-based confidence was used.",
  error: "The AI answer could not be used; the rule-based confidence was used.",
};

function ItemDrawer({ id, onClose, onDecided }: { id: number; onClose: () => void; onDecided: (msg: string) => void }) {
  const { can } = useAuth();
  const item = useAsync(() => api.get<WorkItem>(`/items/${id}`), [id]);
  const i = item.data;
  const [override, setOverride] = useState<string>("");
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [allFloors, setAllFloors] = useState<Site[] | null>(null);
  const candidates: { site_id: string; hierarchy: string; score: number }[] = i?.payload?.candidate_floors || [];

  useEffect(() => {
    if (i?.blocked_reason && candidates[0] && !override) setOverride(candidates[0].site_id);
  }, [i?.id]);

  const loadAll = () => i?.source_id && api.get<Site[]>(`/sources/${i.source_id}/sites?type=floor`).then(setAllFloors);
  const floorOptions = useMemo(() => {
    const seen = new Set(candidates.map((c) => c.site_id));
    return [...candidates.map((c) => ({ id: c.site_id, label: `${c.hierarchy} (score ${c.score})` })),
      ...(allFloors || []).filter((f) => !seen.has(f.id)).map((f) => ({ id: f.id, label: f.hierarchy }))];
  }, [candidates, allFloors]);

  const siteAction = i?.action_type === "assign_site" || i?.action_type === "reassign_site";
  const overrideKey = siteAction ? "site_id" : i?.action_type === "rename_ap" ? "hostname_new" : i?.action_type === "set_role" ? "role" : null;

  const act = async (decision: "approve" | "reject", withOverride: boolean) => {
    setErr(null);
    try {
      const r = await api.post<{ updated: number[]; errors: { error: string }[] }>("/items/decide", {
        decision, ids: [id], note: note || undefined,
        override: withOverride && overrideKey ? { [overrideKey]: override } : undefined,
      });
      if (r.errors.length) setErr(r.errors[0].error);
      else onDecided(`Item ${decision === "approve" ? "approved" : "rejected"}.`);
    } catch (e: any) {
      setErr(e.message);
    }
  };

  return (
    <Drawer onClose={onClose}>
      {!i ? <ErrorLine error={item.error} /> : (
        <>
          <div className="row">
            <h2 style={{ margin: 0 }}>{i.target_label}</h2>
            <div className="spacer" />
            <button onClick={onClose}>Close</button>
          </div>
          <p className="sub">{i.group_label}</p>
          <div className="grid cols-4" style={{ margin: "14px 0" }}>
            <div><div className="small muted">Confidence</div><Confidence value={i.confidence} /></div>
            <div><div className="small muted">Rule-based</div><Confidence value={i.rule_confidence} small /></div>
            {i.llm_status !== "off" && (
              <div>
                <div className="small muted">AI review{i.llm_provider && ` (${i.llm_provider === "local" ? "Local" : "Cloud"})`}</div>
                {i.llm_status === "ok" ? <Confidence value={i.llm_confidence} small />
                  : <span className={`small ${i.llm_status === "unavailable" || i.llm_status === "error" ? "err" : "muted"}`}
                    title={LLM_STATUS_HELP[i.llm_status]}>{LLM_STATUS_TEXT[i.llm_status] || i.llm_status}</span>}
              </div>
            )}
            <div><div className="small muted">Status</div><Status value={i.status} /></div>
          </div>
          <div className="card">
            <PayloadDetails item={i} />
          </div>
          {i.blocked_reason && <div className="banner warn">{i.blocked_reason}</div>}
          <h3>Rationale</h3>
          <p>{i.rationale}</p>
          {!!i.concerns?.length && (
            <>
              <h3>Concerns</h3>
              <ul>{i.concerns.map((c, n) => <li key={n}>{c}</li>)}</ul>
            </>
          )}
          <h3>Evidence</h3>
          {(i.evidence || []).map((e) => (
            <div key={e.signal} className={`evidence ${e.agrees ? "agree" : ""} ${e.available ? "" : "na"}`}>
              <div className="row"><b>{e.label}</b><span className="small muted">weight {e.weight}</span><div className="spacer" />{e.agrees && <span className="badge ok">supports</span>}</div>
              <div className="small">{e.detail}</div>
              {!!e.buildings.length && <div className="small muted">Building: {e.buildings.map((b) => `${b.site} (${b.strength})`).join(", ")}</div>}
              {!!e.floors.length && <div className="small muted">Floor: {e.floors.map((b) => `${b.site} (${b.strength})`).join(", ")}</div>}
            </div>
          ))}
          {i.status === "proposed" && can("operator") && (
            <div className="card" style={{ marginTop: 16 }}>
              <h2>Decision</h2>
              <ErrorLine error={err} />
              {siteAction && (
                <>
                  <label className="field">Override target floor (optional unless a choice is required)
                    <select value={override} onChange={(e) => setOverride(e.target.value)}>
                      <option value="">Use proposal: {i.payload?.site_hierarchy || "none"}</option>
                      {floorOptions.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
                    </select>
                  </label>
                  {!allFloors && <button className="link small" onClick={loadAll}>Show all floors</button>}
                </>
              )}
              {i.action_type === "rename_ap" && (
                <label className="field">Override new name (optional)
                  <input value={override} placeholder={i.payload?.hostname_new} onChange={(e) => setOverride(e.target.value.trim())} />
                </label>
              )}
              {i.action_type === "set_role" && (
                <label className="field">Override role (optional)
                  <select value={override} onChange={(e) => setOverride(e.target.value)}>
                    <option value="">Use proposal: {i.payload?.role}</option>
                    {["ACCESS", "DISTRIBUTION", "CORE"].map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
                </label>
              )}
              {i.action_type === "report" && <p className="small muted">Report item: rejecting it acknowledges the finding.</p>}
              <label className="field" style={{ marginTop: 10 }}>Note
                <textarea value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional. For rejections, say why." />
              </label>
              <div className="row end">
                <button className="danger" onClick={() => act("reject", false)}>Reject</button>
                <button className="ok" disabled={!override && !!i.blocked_reason} onClick={() => act("approve", !!override)}>
                  {override ? "Approve with override" : "Approve"}
                </button>
              </div>
            </div>
          )}
          {(i.exec_result || i.outcome || i.decision_note) && (
            <>
              <h3>Outcome</h3>
              <div className="kv small">
                {i.decided_by && <><div className="k">Decided by</div><div>{i.decided_by} {fmtDate(i.decided_at)}</div></>}
                {i.decision_note && <><div className="k">Note</div><div>{i.decision_note}</div></>}
                {i.verify_status && <><div className="k">Verification</div><div><Status value={i.verify_status} /></div></>}
              </div>
              {i.exec_result && <pre>{JSON.stringify(i.exec_result, null, 2)}</pre>}
              {i.outcome && <pre>{JSON.stringify(i.outcome, null, 2)}</pre>}
            </>
          )}
        </>
      )}
    </Drawer>
  );
}
