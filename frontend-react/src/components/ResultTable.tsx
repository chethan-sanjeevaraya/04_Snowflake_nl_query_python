interface Props {
  columns: string[];
  rows: Record<string, unknown>[];
  msgIndex: number;
}

const TALL_TABLE_ROW_THRESHOLD = 12;

function toCsv(columns: string[], rows: Record<string, unknown>[]): string {
  const escape = (v: unknown) => {
    if (v === null || v === undefined) return "";
    const s = String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [columns.map(escape).join(",")];
  for (const row of rows) {
    lines.push(columns.map((c) => escape(row[c])).join(","));
  }
  return lines.join("\n");
}

export function ResultTable({ columns, rows, msgIndex }: Props) {
  if (rows.length === 0) {
    return <div className="info-banner">The query ran successfully but matched no rows.</div>;
  }

  const cols = columns.length > 0 ? columns : Object.keys(rows[0] ?? {});
  const isTall = rows.length > TALL_TABLE_ROW_THRESHOLD;

  function downloadCsv() {
    const csv = toCsv(cols, rows);
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `query_result_${msgIndex}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="result-table-wrap">
      <div className={"table-scroll" + (isTall ? " table-scroll--tall" : "")}>
        <table className="result-table">
          <thead>
            <tr>
              {cols.map((c) => (
                <th key={c}>{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                {cols.map((c) => (
                  <td key={c}>{row[c] === null || row[c] === undefined ? "" : String(row[c])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <button className="btn btn-secondary btn-sm" onClick={downloadCsv}>
        Download CSV
      </button>
    </div>
  );
}
