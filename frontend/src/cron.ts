export const CRON_PRESETS: { label: string; cron: string }[] = [
  { label: "Every 15 minutes", cron: "*/15 * * * *" },
  { label: "Every hour", cron: "0 * * * *" },
  { label: "Every 4 hours", cron: "0 */4 * * *" },
  { label: "Every 6 hours", cron: "0 */6 * * *" },
  { label: "Daily at 02:00 UTC", cron: "0 2 * * *" },
  { label: "Weekdays at 06:00 UTC", cron: "0 6 * * 1-5" },
  { label: "Weekly, Sunday 03:00 UTC", cron: "0 3 * * 0" },
];

const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const pad = (v: string) => v.padStart(2, "0");

/** Plain-language version of the common cron shapes; null when the expression is something else. */
export function describeCron(expr: string): string | null {
  const f = expr.trim().split(/\s+/);
  if (f.length !== 5) return null;
  const [m, h, dom, mon, dow] = f;
  const num = (v: string) => /^\d+$/.test(v);
  const every = (v: string) => (/^\*\/\d+$/.test(v) ? Number(v.slice(2)) : null);
  if (dom !== "*" || mon !== "*") return null;
  let days = "";
  if (dow === "1-5") days = " on weekdays";
  else if (num(dow) && Number(dow) <= 7) days = ` every ${DAYS[Number(dow) % 7]}`;
  else if (dow !== "*") return null;
  if (every(m) && h === "*") return `Every ${every(m)} minutes${days}`;
  if (num(m) && h === "*") return `Every hour at :${pad(m)}${days}`;
  if (num(m) && every(h)) return `Every ${every(h)} hours at :${pad(m)}${days}`;
  if (num(m) && num(h)) return `${days ? days.trim().replace(/^./, (c) => c.toUpperCase()) : "Daily"} at ${pad(h)}:${pad(m)} UTC`;
  return null;
}
