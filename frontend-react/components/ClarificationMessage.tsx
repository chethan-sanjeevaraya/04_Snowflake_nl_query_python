import type { QueryResult } from "../types";

interface Props {
  result: QueryResult;
  isLatest: boolean;
  onPick: (candidate: string) => void;
}

/** The Lambda couldn't confidently pick a table and is asking which one was
 * meant, instead of just erroring out (see backend/lambda_function.py's
 * needs_clarification response). Clicking a candidate -- or just typing a
 * table name/number into the chat box -- resolves it server-side via the
 * session; there's no need to repeat the original question. Buttons only
 * render on the LATEST such message, matching frontend/app.py's behaviour. */
export function ClarificationMessage({ result, isLatest, onPick }: Props) {
  const p = result.payload;
  const candidates = p.candidates ?? [];

  return (
    <div>
      <p className="msg-text">{p.message || "Which table did you mean?"}</p>
      <div className="meta-line">
        <span className="meta-time">{result.elapsed.toFixed(1)}s</span>
      </div>
      {isLatest && candidates.length > 0 && (
        <div className="candidate-row">
          {candidates.map((c) => (
            <button key={c} className="btn btn-secondary btn-sm" onClick={() => onPick(c)}>
              {c}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
