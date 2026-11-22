// Blue dots for UI changes: a dot sits on every menu item (and its group) that leads to a change the user
// has not seen yet. Opening the page marks its changes as seen; the page shows what changed once.
import { createContext, ReactNode, useContext, useEffect, useMemo, useState } from "react";
import { useLocation } from "react-router-dom";

export interface Change {
  id: string;
  /** Page the change is on; a trailing "/*" also matches pages below it. */
  path: string;
  text: string;
  admin?: boolean;
}

// Every entry shows a dot to each user who has not opened the page since, so drop entries after a few releases.
export const CHANGES: Change[] = [
  { id: "2026.11-history", path: "/history", text: "Activity is now called History: everything that was decided or automated." },
  { id: "2026.11-task-history", path: "/runs", text: "Run history is now called Expert Task history." },
  { id: "2026.11-inbox-groups", path: "/inbox", text: "The By group table is simpler. Bulk approve and reject are in the Items tab." },
  { id: "2026.11-tasks-accordion", path: "/tasks", admin: true, text: "Categories collapse to their headline, and so do the sections on each task page; one is open at a time." },
  { id: "2026.11-automation", path: "/automation", admin: true, text: "New page: the kill switch and the global autonomy cap, moved here from Settings." },
  { id: "2026.11-ai", path: "/ai", admin: true, text: "New page: AI assistance, moved here from Settings." },
  { id: "2026.11-updates", path: "/updates", admin: true, text: "New page: Updates, moved here from Settings. Versions now follow the calendar: year.month.revision." },
  { id: "2026.11-ap-uplink", path: "/tasks", admin: true, text: "New Expert Task: AP uplink health finds APs on half duplex or 100 Mbit switch ports (report only)." },
  { id: "2026.11-users-delete", path: "/users", admin: true, text: "Users can now be deleted. Their decisions and audit entries keep the name." },
];

const matches = (c: Change, pathname: string) =>
  c.path.endsWith("/*") ? pathname.startsWith(c.path.slice(0, -1)) : pathname === c.path;

interface NewsCtx {
  /** True if a page at or below one of the paths has unseen changes. */
  hasNew: (...paths: string[]) => boolean;
  /** Changes on the current page that were unseen when it was opened. */
  here: Change[];
}

const Ctx = createContext<NewsCtx>({ hasNew: () => false, here: [] });
export const useNews = () => useContext(Ctx);

export function NewsProvider({ username, admin, children }: { username: string; admin: boolean; children: ReactNode }) {
  const key = `aiops.seen.${username}`;
  const [seen, setSeen] = useState<Set<string>>(() => new Set(JSON.parse(localStorage.getItem(key) || "[]")));
  const [here, setHere] = useState<Change[]>([]);
  const { pathname } = useLocation();
  const visible = useMemo(() => CHANGES.filter((c) => admin || !c.admin), [admin]);

  useEffect(() => {
    const fresh = visible.filter((c) => !seen.has(c.id) && matches(c, pathname));
    setHere(fresh);
    if (!fresh.length) return;
    const next = new Set(seen);
    fresh.forEach((c) => next.add(c.id));
    localStorage.setItem(key, JSON.stringify([...next].filter((id) => CHANGES.some((c) => c.id === id))));
    setSeen(next);
  }, [pathname]);

  const hasNew = (...paths: string[]) =>
    visible.some((c) => !seen.has(c.id) && paths.some((p) => c.path === p || c.path.startsWith(`${p}/`)));
  return <Ctx.Provider value={{ hasNew, here }}>{children}</Ctx.Provider>;
}

export function NewDot({ title = "New" }: { title?: string }) {
  return <span className="new-dot" title={title} aria-label={title} />;
}

/** Shown once at the top of a page that has changes the user has not seen. */
export function NewHere() {
  const { here } = useNews();
  if (!here.length) return null;
  return (
    <div className="banner new-here">
      <NewDot />
      <div>{here.map((c) => <div key={c.id}>{c.text}</div>)}</div>
    </div>
  );
}
