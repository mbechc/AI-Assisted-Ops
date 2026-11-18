import React, { Fragment, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useAuth } from "../App";
import { api, fmtDate, qs } from "../api";
import { Confidence, Drawer, ErrorLine, Pager, Status, useAsync } from "../components";
import { Group, Site, Task, WorkItem } from "../types";

const LIMIT = 100;

export default function Review() {
  const [sp, setSp] = useSearchParams();
  const tasks = useAsync(() => api.get<Task[]>("/tasks"), []);
  const taskKey = sp.get("task") || tasks.data?.[0]?.key || "";
  const task = tasks.data?.find((t) => t.key === taskKey);
  const [view, setView] = useState<"groups" | "list">("groups");
  const [status, setStatus] = useState(sp.get("status") || "proposed");
  const [group, setGroup] = useState<string | null>(null);
  const [minC, setMinC] = useState("");
  const [maxC, setMaxC] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [openItem, setOpenItem] = useState<number | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => { setOffset(0); setSelected(new Set()); }, [taskKey, status, group, minC, maxC, q]);

  const groups = useAsync(
    () => (taskKey ? api.get<{ threshold: number; groups: Group[] }>(`/items/groups${qs({ task_key: taskKey, status })}`) : Promise.resolve(null)),
    [taskKey, status], 20000);
  const items = useAsync(
    () => (taskKey ? api.get<{ total: number; items: WorkItem[] }>(`/items${qs({
      task_key: taskKey, status, group: group ?? undefined, min_confidence: minC, max_confidence: maxC, q, limit: LIMIT, offset,
    })}`) : Promise.resolve(null)),
    [taskKey, status, group, minC, maxC, q, offset], 20000);

  const refresh = () => { groups.reload(); items.reload(); };

  const decide = async (body: Record<string, unknown>, label: string) => {
    setErr(null);
    try {
      const r = await api.post<{ updated: number[]; errors: { id: number; error: string }[] }>("/items/decide", body);
      setMsg(`${label}: ${r.updated.length} item(s).` + (r.errors.length ? ` ${r.errors.length} skipped (e.g. ${r.errors[0].error}).` : ""));
      setSelected(new Set());
      refresh();
    } catch (e: any) {
      setErr(e.message);
    }
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Review queue</h1>
          <p className="sub">Approve, override or reject what the recurring tasks propose. Decisions train the confidence calibration.</p>
        </div>
        <div className="row">
          <select value={taskKey} onChange={(e) => { setSp({ task: e.target.value }); setGroup(null); }}>
            {(tasks.data || []).map((t) => <option key={t.key} value={t.key}>{t.name} ({t.counts.proposed || 0})</option>)}
          </select>
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="proposed">Awaiting review</option>
            <option value="approved,executing">Approved / executing</option>
            <option value="done">Done</option>
            <option value="failed">Failed</option>
            <option value="rejected">Rejected</option>
            <option value="superseded">Superseded</option>
            <option value="reported">Reported (L0)</option>
          </select>
        </div>
      </div>
      {msg && <div className="banner info">{msg}</div>}
      <ErrorLine error={err || items.error || groups.error} />
      <div className="tabs">
        <button className={view === "groups" ? "active" : ""} onClick={() => setView("groups")}>By group</button>
        <button className={view === "list" ? "active" : ""} onClick={() => setView("list")}>
          Items{group ? `: ${group}` : ""} ({items.data?.total ?? 0})
        </button>
      </div>
      {view === "groups" ? (
        <GroupsView
          data={groups.data} status={status} taskKey={taskKey}
          onOpen={(g) => { setGroup(g); setView("list"); }}
          onBulk={(g, min) => decide({ decision: "approve", filter: { task_key: taskKey, group: g, min_confidence: min } }, `Approved ${g ?? "group"} >= ${min}`)}
        />
      ) : (
        <div className="card">
          <div className="row" style={{ marginBottom: 10 }}>
            {group !== null && <span className="badge info">{group} <button className="link" onClick={() => setGroup(null)}>x</button></span>}
            <input placeholder="Search name or group" value={q} onChange={(e) => setQ(e.target.value)} />
            <input style={{ width: 90 }} placeholder="Min conf" value={minC} onChange={(e) => setMinC(e.target.value)} />
            <input style={{ width: 90 }} placeholder="Max conf" value={maxC} onChange={(e) => setMaxC(e.target.value)} />
            <div className="spacer" />
            {status === "proposed" && (
              <BulkBar
                selected={selected} task={task}
                onApprove={() => decide({ decision: "approve", ids: [...selected] }, "Approved")}
                onReject={(note) => decide({ decision: "reject", ids: [...selected], note }, "Rejected")}
                onApproveFilter={() => decide({
                  decision: "approve",
                  filter: { task_key: taskKey, group, min_confidence: minC ? Number(minC) : undefined, max_confidence: maxC ? Number(maxC) : undefined },
                }, "Approved all matching")}
              />
            )}
          </div>
          <ItemsTable
            items={items.data?.items || []} selectable={status === "proposed"} selected={selected}
            setSelected={setSelected} onOpen={setOpenItem}
          />
          <Pager total={items.data?.total || 0} limit={LIMIT} offset={offset} onChange={setOffset} />
        </div>
      )}
      {openItem !== null && <ItemDrawer id={openItem} onClose={() => setOpenItem(null)} onDecided={(m) => { setMsg(m); setOpenItem(null); refresh(); }} />}
    </>
  );
}

function GroupsView({ data, status, onOpen, onBulk }: {
  data: { threshold: number; groups: Group[] } | null; status: string; taskKey: string;
  onOpen: (g: string | null) => void; onBulk: (g: string | null, min: number) => void;
}) {
  const { can } = useAuth();
  const [min, setMin] = useState<number | null>(null);
  const threshold = min ?? data?.threshold ?? 95;
  if (!data) return null;
  return (
    <div className="card">
      {status === "proposed" && can("operator") && (
        <div className="row" style={{ marginBottom: 10 }}>
          <span className="muted small">Bulk approve threshold</span>
          <input type="number" style={{ width: 80 }} value={threshold} onChange={(e) => setMin(Number(e.target.value))} />
          <span className="muted small">Items needing a manual choice (blocked) are never bulk-approved.</span>
        </div>
      )}
      <table>
        <thead>
          <tr><th>Group</th><th className="num">Items</th><th className="num">Avg conf.</th><th className="num">Range</th><th className="num">&gt;= threshold</th><th className="num">Need choice</th><th /></tr>
        </thead>
        <tbody>
          {data.groups.map((g) => (
            <tr key={g.group ?? "none"} className="click" onClick={() => onOpen(g.group)}>
              <td>{g.group ?? <span className="muted">none</span>}</td>
              <td className="num">{g.count}</td>
              <td className="num"><Confidence value={Math.round(g.avg_confidence)} small /></td>
              <td className="num small muted">{g.min_confidence}-{g.max_confidence}</td>
              <td className="num">{g.above_threshold}</td>
              <td className="num">{g.blocked || ""}</td>
              <td className="num" onClick={(e) => e.stopPropagation()}>
                {status === "proposed" && can("operator") && (
                  <button className="ok" onClick={() => {
                    if (confirm(`Approve all items in "${g.group}" with confidence >= ${threshold}?`)) onBulk(g.group, threshold);
                  }}>Approve &gt;= {threshold}</button>
                )}
              </td>
            </tr>
          ))}
          {!data.groups.length && <tr><td colSpan={7} className="muted">Nothing here.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

function BulkBar({ selected, onApprove, onReject, onApproveFilter }: {
  selected: Set<number>; task?: Task; onApprove: () => void; onReject: (note: string) => void; onApproveFilter: () => void;
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
      <button onClick={() => confirm("Approve every item matching the current filters (blocked items excluded)?") && onApproveFilter()}>
        Approve all matching
      </button>
    </>
  );
}

function ItemsTable({ items, selectable, selected, setSelected, onOpen }: {
  items: WorkItem[]; selectable: boolean; selected: Set<number>; setSelected: (s: Set<number>) => void; onOpen: (id: number) => void;
}) {
  const allSel = items.length > 0 && items.every((i) => selected.has(i.id));
  const toggle = (id: number) => {
    const s = new Set(selected);
    s.has(id) ? s.delete(id) : s.add(id);
    setSelected(s);
  };
  return (
    <table>
      <thead>
        <tr>
          {selectable && <th><input type="checkbox" checked={allSel} onChange={() => setSelected(allSel ? new Set() : new Set(items.map((i) => i.id)))} /></th>}
          <th>Target</th><th>Proposal</th><th>Confidence</th><th>Status</th><th>Notes</th><th>Updated</th>
        </tr>
      </thead>
      <tbody>
        {items.map((i) => (
          <tr key={i.id} className={`click ${selected.has(i.id) ? "selected" : ""}`} onClick={() => onOpen(i.id)}>
            {selectable && <td onClick={(e) => e.stopPropagation()}><input type="checkbox" checked={selected.has(i.id)} onChange={() => toggle(i.id)} /></td>}
            <td><b>{i.target_label}</b><div className="small muted">{i.group_label}</div></td>
            <td className="small">{i.proposed || <span className="muted">-</span>}</td>
            <td><Confidence value={i.confidence} />{i.llm_status === "ok" && <span className="small muted" title="Reviewed by the local LLM"> AI</span>}</td>
            <td><Status value={i.status} />{i.decided_by === "auto" && <div className="small muted">auto</div>}</td>
            <td className="small">
              {i.blocked_reason ? <span className="badge warn">needs choice</span> : i.concerns?.length ? <span className="muted">{i.concerns.length} concern(s)</span> : ""}
            </td>
            <td className="small muted">{fmtDate(i.updated_at)}</td>
          </tr>
        ))}
        {!items.length && <tr><td colSpan={7} className="muted">No items.</td></tr>}
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
        ["Method", p.method === "history" ? "earlier name from inventory history" : "floor naming convention"],
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
            <div><div className="small muted">Local LLM</div>{i.llm_status === "ok" ? <Confidence value={i.llm_confidence} small /> : <span className="muted small">{i.llm_status}</span>}</div>
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
