import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, type ReactNode } from "react";
import { ApiError, authApi, onUnauthorized, setCsrfToken } from "@/services/api";
import type { Connection, SessionInfo } from "@/types/api";

export const SESSION_KEY = ["session"] as const;

interface AuthValue {
  session: SessionInfo | null;
  isLoading: boolean;
  can: (permission: string) => boolean;
  refresh: () => Promise<void>;
  clear: () => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();

  const clear = useCallback(() => {
    setCsrfToken(null);
    qc.setQueryData(SESSION_KEY, null);
    qc.removeQueries({ predicate: (q) => q.queryKey[0] !== "session" });
  }, [qc]);

  useEffect(() => {
    onUnauthorized(clear);
    return () => onUnauthorized(null);
  }, [clear]);

  const query = useQuery({
    queryKey: SESSION_KEY,
    queryFn: async (): Promise<SessionInfo | null> => {
      try {
        return await authApi.session();
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null;
        throw e;
      }
    },
    staleTime: 60_000,
    retry: false,
  });

  const session = query.data ?? null;
  const value = useMemo<AuthValue>(
    () => ({
      session,
      isLoading: query.isLoading,
      can: (p) => session?.app_permissions.includes(p) ?? false,
      refresh: async () => {
        await qc.invalidateQueries({ queryKey: SESSION_KEY });
      },
      clear,
    }),
    [session, query.isLoading, qc, clear],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}

export function canOn(connection: Pick<Connection, "permissions">, permission: string): boolean {
  return connection.permissions.includes(permission);
}
