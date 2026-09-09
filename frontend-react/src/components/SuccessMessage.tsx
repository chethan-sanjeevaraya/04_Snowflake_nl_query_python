import { useState } from "react";
import type { QueryResult } from "../types";
import { ResultTable } from "./ResultTable";

interface Props {
  result: QueryResult;
  msgIndex: number;
  showSqlExpandedByDefault: boolean;
}

export function SuccessMessage({ result, msgIndex, showSqlExpandedByDefault }: Props) {
  const [sqlOpen, setSqlOpen] = useState(showSqlExpandedByDefault);
  const p = result.payload;

  const metaBits: string[] = [];
  if (p.database && p.schema) metaBits.push(`${p.database}.${p.schema}`);

  return (
    <div>
      <p className="msg-text">{p.answer || "Query completed."}</p>

      {p.truncated && (
        <div className="warning-banner">
          Showing {p.row_count} of {p.total_matching_rows} matching rows.
        </div>
      )}
      {p.note && <div className="caption">{p.note}</div>}

      <div className="meta-line">
        {metaBits.map((b) => (
          <code key={b} className="chip">
            {b}
          </code>
        ))}
        {p.tables_used && p.tables_used.length > 0 && (
          <span className="meta-tables">
            tables:{" "}
            {p.tables_used.map((t) => (
              <code key={t} className="chip">
                {t}
              </code>
            ))}
          </span>
        )}
        <span className="meta-time">{result.elapsed.toFixed(1)}s</span>
      </div>

      {p.sql && (
        <div className="expander">
          <button className="expander-toggle" onClick={() => setSqlOpen((v) => !v)}>
            {sqlOpen ? "▾" : "▸"} SQL
          </button>
          {sqlOpen && <pre className="code-block">{p.sql}</pre>}
        </div>
      )}

      <ResultTable columns={p.columns ?? []} rows={p.rows ?? []} msgIndex={msgIndex} />
    </div>
  );
}
