import type { Environment } from "@/types/api";

export const ENVIRONMENTS: Environment[] = ["production", "staging", "development", "other"];

export const ENV_STYLES: Record<Environment, { badge: string; bar: string }> = {
  production: {
    badge: "bg-destructive text-destructive-foreground",
    bar: "bg-destructive text-destructive-foreground",
  },
  staging: { badge: "bg-warning text-black", bar: "bg-warning text-black" },
  development: { badge: "bg-success text-black", bar: "bg-success text-black" },
  other: { badge: "bg-muted text-muted-foreground", bar: "bg-muted text-muted-foreground" },
};

export const DEFAULT_PORT = {
  postgresql: 5432,
  mysql: 3306,
  mariadb: 3306,
  mssql: 1433,
  clickhouse: 8123,
  sqlite: 1,
  redis: 6379,
  elasticsearch: 9200,
  mongodb: 27017,
} as const;
