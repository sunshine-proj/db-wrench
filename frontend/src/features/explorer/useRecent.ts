import { useCallback, useState } from "react";

export interface RecentItem {
  to: string;
  label: string;
  connection: string;
  at: number;
}

const KEY = "dbwrench.recent";
const MAX = 8;

function read(): RecentItem[] {
  try {
    const raw = localStorage.getItem(KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? (parsed as RecentItem[]).slice(0, MAX) : [];
  } catch {
    return []; // storage may be unavailable (private mode); this is only a convenience
  }
}

/** Per-viewer convenience list of recently opened objects (never sent to the server). */
export function useRecent() {
  const [items, setItems] = useState<RecentItem[]>(read);
  const push = useCallback((item: Omit<RecentItem, "at">) => {
    const next = [{ ...item, at: Date.now() }, ...read().filter((r) => r.to !== item.to)].slice(
      0,
      MAX,
    );
    try {
      localStorage.setItem(KEY, JSON.stringify(next));
    } catch {
      /* ignore */
    }
    setItems(next);
  }, []);
  return { items, push };
}
