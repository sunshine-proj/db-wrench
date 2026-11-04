import { lazy, Suspense } from "react";
import { Textarea } from "@/components/ui/form";
import { Spinner } from "@/components/ui/misc";
import { t } from "@/i18n";
import type { CodeEditorProps } from "./CodeEditor";

// Monaco is large; it is only fetched when a workspace is opened.
const CodeEditor = lazy(() => import("./CodeEditor"));

export function LazyEditor(props: CodeEditorProps) {
  return (
    <Suspense
      fallback={
        <div className="space-y-2">
          <Spinner label={t("workspace.editorLoading")} />
          <Textarea
            aria-label={props.ariaLabel}
            value={props.value}
            onChange={(e) => props.onChange(e.target.value)}
            rows={8}
          />
        </div>
      }
    >
      <CodeEditor {...props} />
    </Suspense>
  );
}
