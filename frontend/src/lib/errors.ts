import { ApiError } from "@/services/api";

export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    const fields = e.problem.errors?.map((x) => `${x.loc.slice(1).join(".")}: ${x.msg}`);
    const base = fields?.length ? fields.join("; ") : e.message;
    return base;
  }
  return e instanceof Error ? e.message : String(e);
}

export function requestId(e: unknown): string | undefined {
  return e instanceof ApiError ? e.requestId : undefined;
}
