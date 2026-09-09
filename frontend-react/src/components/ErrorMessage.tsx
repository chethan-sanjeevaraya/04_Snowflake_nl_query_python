import { useState } from "react";
import type { QueryResult } from "../types";

interface Props {
  result: QueryResult;
}

export function ErrorMessage({ result }: Props) {
  const [detailOpen, setDetailOpen] = useState(false);
  const p = result.payload;

  return (
    <div>
      <div className="error-banner">{result.error}</div>

      {result.detail && (
        <div className="expander">
          <button className="expander-toggle" onClick={() => setDetailOpen((v) => !v)}>
            {detailOpen ? "▾" : "▸"} Detail
          </button>
          {detailOpen && <pre className="code-block code-block--wrap">{result.detail}</pre>}
        </div>
      )}

      {p.reason && <div className="caption">Reason: {p.reason as string}</div>}

      {p.sql && (
        <div className="expander">
          <button className="expander-toggle" disabled>
            ▾ Generated SQL
          </button>
          <pre className="code-block">{p.sql as string}</pre>
        </div>
      )}

      {p.available_tables && p.available_tables.length > 0 && (
        <details className="expander">
          <summary>Available tables ({p.available_tables.length})</summary>
          <div className="caption">{p.available_tables.join(", ")}</div>
        </details>
      )}

      {p.valid_targets && p.valid_targets.length > 0 && (
        <div className="caption">Valid targets: {p.valid_targets.join(", ")}</div>
      )}

      <div className="meta-line">
        <span className="meta-time">{result.elapsed.toFixed(1)}s</span>
      </div>
    </div>
  );
}
