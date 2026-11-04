import { useCallback, useState } from "react";

export interface QueryTab {
  id: string;
  title: string;
  text: string;
  database?: string;
  schema?: string;
}

function load(key: string, initial: () => QueryTab): { tabs: QueryTab[]; active: string } {
  try {
    const raw = localStorage.getItem(key);
    const parsed = raw ? (JSON.parse(raw) as { tabs?: QueryTab[]; active?: string }) : null;
    if (parsed?.tabs?.length)
      return { tabs: parsed.tabs, active: parsed.active ?? parsed.tabs[0]!.id };
  } catch {
    /* storage unavailable or corrupt: start fresh */
  }
  const first = initial();
  return { tabs: [first], active: first.id };
}

/** Multiple editor tabs, remembered per connection in this browser only (never sent anywhere). */
export function useTabs(storageKey: string, blank: () => string) {
  const make = useCallback(
    (n: number): QueryTab => ({ id: crypto.randomUUID(), title: `Query ${n}`, text: blank() }),
    [blank],
  );
  const [state, setState] = useState(() => load(storageKey, () => make(1)));

  const persist = useCallback(
    (next: { tabs: QueryTab[]; active: string }) => {
      setState(next);
      try {
        localStorage.setItem(storageKey, JSON.stringify(next));
      } catch {
        /* ignore: persistence is a convenience */
      }
    },
    [storageKey],
  );

  const active = state.tabs.find((t) => t.id === state.active) ?? state.tabs[0]!;
  return {
    tabs: state.tabs,
    active,
    select: (id: string) => persist({ ...state, active: id }),
    update: (patch: Partial<QueryTab>) =>
      persist({
        ...state,
        tabs: state.tabs.map((t) => (t.id === active.id ? { ...t, ...patch } : t)),
      }),
    add: (init?: Partial<QueryTab>) => {
      const tab = { ...make(state.tabs.length + 1), ...init };
      persist({ tabs: [...state.tabs, tab], active: tab.id });
    },
    close: (id: string) => {
      if (state.tabs.length === 1) return;
      const tabs = state.tabs.filter((t) => t.id !== id);
      persist({ tabs, active: state.active === id ? tabs[0]!.id : state.active });
    },
  };
}
