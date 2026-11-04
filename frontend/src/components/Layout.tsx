import { useMutation } from "@tanstack/react-query";
import {
  Activity,
  Database,
  Globe,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Network,
  Plug,
  ScrollText,
  Shield,
  SquareTerminal,
  Users,
} from "lucide-react";
import type { ComponentType, ReactNode } from "react";
import { Navigate, NavLink, Outlet, useLocation } from "react-router-dom";
import { cn } from "@/components/cn";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/misc";
import { EnrollmentRequired, ForcePasswordChange } from "@/features/authentication/Gates";
import { useAuth } from "@/hooks/useAuth";
import { t, type MessageKey } from "@/i18n";
import { authApi } from "@/services/api";

interface NavItem {
  to: string;
  label: MessageKey;
  icon: ComponentType<{ className?: string }>;
  permission?: string;
  end?: boolean;
}

const NAV: NavItem[] = [
  { to: "/", label: "nav.dashboard", icon: LayoutDashboard, end: true },
  { to: "/connections", label: "nav.connections", icon: Plug },
  { to: "/explorer", label: "nav.explorer", icon: Database },
  { to: "/workspace", label: "nav.workspace", icon: SquareTerminal },
  { to: "/admin/users", label: "nav.users", icon: Users, permission: "app.manage_users" },
  { to: "/admin/roles", label: "nav.roles", icon: Shield, permission: "app.manage_roles" },
  { to: "/admin/network", label: "nav.network", icon: Network, permission: "app.manage_settings" },
  { to: "/audit", label: "nav.audit", icon: ScrollText, permission: "audit.view" },
  { to: "/account", label: "nav.account", icon: KeyRound },
];

/** Gate for every authenticated route: session, MFA state and forced password change. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { session, isLoading } = useAuth();
  const location = useLocation();

  if (isLoading) {
    return (
      <div className="grid min-h-screen place-items-center">
        <Spinner label={t("common.loading")} />
      </div>
    );
  }
  if (!session) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  if (session.mfa_state === "pending_verify") return <Navigate to="/login" replace />;
  if (session.mfa_state === "pending_enroll") return <EnrollmentRequired />;
  if (session.password_change_required) return <ForcePasswordChange />;
  return <>{children}</>;
}

export function AppLayout() {
  const { session, can, clear } = useAuth();
  const logout = useMutation({
    mutationFn: () => authApi.logout(),
    onSettled: () => clear(),
  });
  const items = NAV.filter((n) => !n.permission || can(n.permission));

  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:bg-card focus:p-2"
      >
        {t("nav.skip")}
      </a>
      <aside className="border-b bg-card md:sticky md:top-0 md:h-screen md:w-56 md:shrink-0 md:border-b-0 md:border-r">
        <div className="flex items-center gap-2 px-4 py-3">
          <Activity className="size-5 text-primary" aria-hidden />
          <span className="font-semibold tracking-tight">{t("app.name")}</span>
        </div>
        <nav
          aria-label={t("nav.main")}
          className="flex gap-1 overflow-x-auto px-2 pb-2 md:flex-col md:overflow-visible"
        >
          {items.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2 whitespace-nowrap rounded-md px-3 py-2 text-sm font-medium",
                  isActive
                    ? "bg-accent text-accent-foreground"
                    : "text-muted-foreground hover:bg-muted",
                )
              }
            >
              <Icon className="size-4" aria-hidden />
              {t(label)}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto hidden border-t p-3 md:absolute md:bottom-0 md:block md:w-56">
          <div className="mb-2 flex items-center gap-2 text-xs text-muted-foreground">
            <Globe className="size-3.5" aria-hidden />
            <span className="truncate" title={session?.user.username}>
              {session?.user.display_name || session?.user.username}
            </span>
          </div>
          <Button variant="outline" size="sm" className="w-full" onClick={() => logout.mutate()}>
            <LogOut /> {t("auth.logout")}
          </Button>
        </div>
        <div className="flex items-center justify-between border-t px-3 py-2 md:hidden">
          <span className="truncate text-xs text-muted-foreground">{session?.user.username}</span>
          <Button variant="ghost" size="sm" onClick={() => logout.mutate()}>
            <LogOut /> {t("auth.logout")}
          </Button>
        </div>
      </aside>
      <main id="main" className="min-w-0 flex-1">
        <Outlet />
      </main>
    </div>
  );
}
