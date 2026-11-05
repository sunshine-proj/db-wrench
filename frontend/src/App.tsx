import { lazy, Suspense, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppLayout, RequireAuth } from "@/components/Layout";
import { Alert, Spinner } from "@/components/ui/misc";
import { AccountPage } from "@/features/authentication/AccountPage";
import { LoginPage } from "@/features/authentication/LoginPage";
import { SetupPage } from "@/features/authentication/SetupPage";
import { ConnectionsPage } from "@/features/connections/ConnectionsPage";
import { DashboardPage } from "@/features/dashboard/DashboardPage";
import { useAuth } from "@/hooks/useAuth";
import { t } from "@/i18n";

// Heavier route groups are loaded on demand to keep the first load small.
const UsersPage = lazy(() =>
  import("@/features/administration/UsersPage").then((m) => ({ default: m.UsersPage })),
);
const RolesPage = lazy(() =>
  import("@/features/administration/RolesPage").then((m) => ({ default: m.RolesPage })),
);
const NetworkPolicyPage = lazy(() =>
  import("@/features/administration/NetworkPolicyPage").then((m) => ({
    default: m.NetworkPolicyPage,
  })),
);
const ApprovalsPage = lazy(() =>
  import("@/features/governance/ApprovalsPage").then((m) => ({ default: m.ApprovalsPage })),
);
const GovernancePage = lazy(() =>
  import("@/features/governance/GovernancePage").then((m) => ({ default: m.GovernancePage })),
);
const IntegrationsPage = lazy(() =>
  import("@/features/administration/IntegrationsPage").then((m) => ({
    default: m.IntegrationsPage,
  })),
);
const TokensPage = lazy(() =>
  import("@/features/settings/TokensPage").then((m) => ({ default: m.TokensPage })),
);
const PrincipalsPage = lazy(() =>
  import("@/features/administration/PrincipalsPage").then((m) => ({ default: m.PrincipalsPage })),
);
const AuditPage = lazy(() =>
  import("@/features/audit/AuditPage").then((m) => ({ default: m.AuditPage })),
);
const ExplorerIndex = lazy(() =>
  import("@/features/explorer/ExplorerLayout").then((m) => ({ default: m.ExplorerIndex })),
);
const ExplorerLayout = lazy(() =>
  import("@/features/explorer/ExplorerLayout").then((m) => ({ default: m.ExplorerLayout })),
);
const ConnectionHome = lazy(() =>
  import("@/features/explorer/Views").then((m) => ({ default: m.ConnectionHome })),
);
const SchemaOverview = lazy(() =>
  import("@/features/explorer/Views").then((m) => ({ default: m.SchemaOverview })),
);
const TableView = lazy(() =>
  import("@/features/explorer/Views").then((m) => ({ default: m.TableView })),
);
const WorkspaceIndex = lazy(() =>
  import("@/features/sql-workspace/WorkspaceIndex").then((m) => ({ default: m.WorkspaceIndex })),
);
const SqlWorkspace = lazy(() =>
  import("@/features/sql-workspace/SqlWorkspace").then((m) => ({ default: m.SqlWorkspace })),
);
const MongoWorkspace = lazy(() =>
  import("@/features/mongo-workspace/MongoWorkspace").then((m) => ({ default: m.MongoWorkspace })),
);
const CollectionView = lazy(() =>
  import("@/features/explorer/Views").then((m) => ({ default: m.CollectionView })),
);

function RequirePermission({ permission, children }: { permission: string; children: ReactNode }) {
  const { can } = useAuth();
  if (!can(permission)) {
    return (
      <div className="p-6">
        <Alert variant="warning" title={t("common.forbidden")}>
          {t("common.forbiddenBody")}
        </Alert>
      </div>
    );
  }
  return <>{children}</>;
}

export function App() {
  return (
    <Suspense
      fallback={
        <div className="p-6">
          <Spinner label={t("common.loading")} />
        </div>
      }
    >
      <Routes>
        <Route path="/setup" element={<SetupPage />} />
        <Route path="/login" element={<LoginPage />} />
        <Route
          element={
            <RequireAuth>
              <AppLayout />
            </RequireAuth>
          }
        >
          <Route index element={<DashboardPage />} />
          <Route path="connections" element={<ConnectionsPage />} />
          <Route path="explorer">
            <Route index element={<ExplorerIndex />} />
            <Route path=":cid" element={<ExplorerLayout />}>
              <Route index element={<ConnectionHome />} />
              <Route path="s/:db/:schema" element={<SchemaOverview />} />
              <Route path="s/:db/:schema/:table" element={<TableView />} />
              <Route path="d/:db/:table" element={<TableView />} />
              <Route path="m/:db/:coll" element={<CollectionView />} />
              <Route path="access" element={<PrincipalsPage />} />
            </Route>
          </Route>
          <Route path="workspace">
            <Route index element={<WorkspaceIndex />} />
            <Route path="sql/:cid" element={<SqlWorkspace />} />
            <Route path="mongo/:cid" element={<MongoWorkspace />} />
          </Route>
          <Route
            path="admin/users"
            element={
              <RequirePermission permission="app.manage_users">
                <UsersPage />
              </RequirePermission>
            }
          />
          <Route
            path="admin/roles"
            element={
              <RequirePermission permission="app.manage_roles">
                <RolesPage />
              </RequirePermission>
            }
          />
          <Route
            path="admin/network"
            element={
              <RequirePermission permission="app.manage_settings">
                <NetworkPolicyPage />
              </RequirePermission>
            }
          />
          <Route path="approvals" element={<ApprovalsPage />} />
          <Route
            path="admin/governance"
            element={
              <RequirePermission permission="app.manage_settings">
                <GovernancePage />
              </RequirePermission>
            }
          />
          <Route
            path="admin/integrations"
            element={
              <RequirePermission permission="app.manage_settings">
                <IntegrationsPage />
              </RequirePermission>
            }
          />
          <Route
            path="tokens"
            element={
              <RequirePermission permission="app.manage_tokens">
                <TokensPage />
              </RequirePermission>
            }
          />
          <Route
            path="audit"
            element={
              <RequirePermission permission="audit.view">
                <AuditPage />
              </RequirePermission>
            }
          />
          <Route path="account" element={<AccountPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Suspense>
  );
}
