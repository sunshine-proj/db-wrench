import { useCallback, useState } from "react";

/** Cursor stack: the server only moves forward, so earlier cursors are remembered here. */
export function usePaging() {
  const [stack, setStack] = useState<(string | null)[]>([null]);
  const cursor = stack[stack.length - 1] ?? null;
  const page = stack.length;
  const next = useCallback((c: string) => setStack((s) => [...s, c]), []);
  const prev = useCallback(() => setStack((s) => (s.length > 1 ? s.slice(0, -1) : s)), []);
  const reset = useCallback(() => setStack([null]), []);
  return { cursor, page, next, prev, reset, hasPrev: stack.length > 1 };
}
