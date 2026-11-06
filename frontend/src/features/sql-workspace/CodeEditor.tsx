import { useEffect, useRef } from "react";
import { monaco } from "./monaco";

export interface CodeEditorProps {
  value: string;
  onChange: (value: string) => void;
  language: "pgsql" | "mysql" | "sql" | "redis" | "json";
  ariaLabel: string;
  /** Ctrl/Cmd+Enter */
  onRun?: () => void;
  height?: string;
}

const prefersDark = () => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;

export default function CodeEditor({
  value,
  onChange,
  language,
  ariaLabel,
  onRun,
  height = "16rem",
}: CodeEditorProps) {
  const host = useRef<HTMLDivElement>(null);
  const editor = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const handlers = useRef({ onChange, onRun });
  handlers.current = { onChange, onRun };

  useEffect(() => {
    if (!host.current) return;
    const ed = monaco.editor.create(host.current, {
      value,
      language,
      ariaLabel,
      automaticLayout: true,
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      fontSize: 13,
      tabSize: 2,
      wordWrap: "on",
      theme: prefersDark() ? "vs-dark" : "vs",
      renderWhitespace: "none",
      fixedOverflowWidgets: true,
    });
    editor.current = ed;
    const sub = ed.onDidChangeModelContent(() => handlers.current.onChange(ed.getValue()));
    ed.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => handlers.current.onRun?.());
    const media = window.matchMedia?.("(prefers-color-scheme: dark)");
    const onTheme = () => monaco.editor.setTheme(prefersDark() ? "vs-dark" : "vs");
    media?.addEventListener("change", onTheme);
    return () => {
      media?.removeEventListener("change", onTheme);
      sub.dispose();
      ed.dispose();
      editor.current = null;
    };
    // The editor is created once per language; value changes are synced below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [language]);

  useEffect(() => {
    const ed = editor.current;
    if (ed && ed.getValue() !== value) ed.setValue(value);
  }, [value]);

  return <div ref={host} style={{ height }} className="overflow-hidden rounded-md border" />;
}
