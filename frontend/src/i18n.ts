/**
 * Minimal i18n layer. Every user-visible string lives in `messages` and is referenced by a
 * typed key, so a missing key is a compile error. Add locales by providing another catalogue.
 */
import { messages as en } from "./messages.en";

export type MessageKey = keyof typeof en;

let catalogue: Record<MessageKey, string> = en;

export function setCatalogue(next: Record<MessageKey, string>): void {
  catalogue = next;
}

export function t(key: MessageKey, vars?: Record<string, string | number>): string {
  const template = catalogue[key] ?? key;
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (_, name: string) => String(vars[name] ?? `{${name}}`));
}
