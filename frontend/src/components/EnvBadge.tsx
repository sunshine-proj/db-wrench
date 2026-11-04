import { cn } from "@/components/cn";
import { ENV_STYLES } from "@/lib/environment";
import type { Environment } from "@/types/api";

export function EnvBadge({ env, className }: { env: Environment; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold uppercase tracking-wide",
        ENV_STYLES[env].badge,
        className,
      )}
    >
      {env}
    </span>
  );
}

/** Persistent, full-width environment indicator for every data-bearing view. */
export function EnvBanner({
  env,
  name,
  readOnly,
}: {
  env: Environment;
  name: string;
  readOnly: boolean;
}) {
  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-x-3 px-4 py-1.5 text-xs font-medium",
        ENV_STYLES[env].bar,
      )}
      role="note"
      data-testid="env-banner"
    >
      <span className="font-bold uppercase tracking-wider">{env}</span>
      <span>{name}</span>
      {readOnly && <span className="opacity-90">· read-only</span>}
    </div>
  );
}
