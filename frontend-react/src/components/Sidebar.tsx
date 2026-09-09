interface Props {
  apiUrl: string;
  onApiUrlChange: (v: string) => void;
  apiKey: string;
  onApiKeyChange: (v: string) => void;
  timeoutSeconds: number;
  onTimeoutChange: (v: number) => void;

  targets: string[];
  targetError: string;
  discovering: boolean;
  onDiscoverTargets: () => void;
  selectedTarget: string; // "" = Lambda default, else "DATABASE.SCHEMA"
  onSelectedTargetChange: (v: string) => void;
  manualDatabase: string;
  onManualDatabaseChange: (v: string) => void;
  manualSchema: string;
  onManualSchemaChange: (v: string) => void;

  tableHint: string;
  onTableHintChange: (v: string) => void;

  showSqlExpandedByDefault: boolean;
  onShowSqlExpandedByDefaultChange: (v: boolean) => void;
  sessionId: string;
  onClearChat: () => void;
}

export function Sidebar(props: Props) {
  return (
    <aside className="sidebar">
      <section>
        <h2 className="sidebar-heading">Connection</h2>
        <label className="field">
          <span>API Gateway invoke URL</span>
          <input
            type="text"
            value={props.apiUrl}
            onChange={(e) => props.onApiUrlChange(e.target.value)}
            placeholder="https://abc123.execute-api.eu-central-1.amazonaws.com/dev/query"
          />
        </label>
        <label className="field">
          <span>API key (optional)</span>
          <input
            type="password"
            value={props.apiKey}
            onChange={(e) => props.onApiKeyChange(e.target.value)}
            placeholder="Sent as x-api-key"
          />
        </label>
        <label className="field">
          <span>Client timeout: {props.timeoutSeconds}s</span>
          <input
            type="range"
            min={15}
            max={180}
            step={5}
            value={props.timeoutSeconds}
            onChange={(e) => props.onTimeoutChange(Number(e.target.value))}
          />
        </label>
      </section>

      <hr />

      <section>
        <h2 className="sidebar-heading">Target</h2>
        <button className="btn btn-secondary btn-block" onClick={props.onDiscoverTargets} disabled={props.discovering}>
          {props.discovering ? "Asking the API…" : "Discover valid targets"}
        </button>

        {props.targets.length > 0 ? (
          <label className="field">
            <span>DATABASE.SCHEMA</span>
            <select value={props.selectedTarget} onChange={(e) => props.onSelectedTargetChange(e.target.value)}>
              <option value="">(Lambda default)</option>
              {props.targets.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <>
            {props.targetError && <p className="caption caption--error">Discovery failed: {props.targetError}</p>}
            <div className="field-row">
              <label className="field">
                <span>DATABASE</span>
                <input
                  type="text"
                  value={props.manualDatabase}
                  onChange={(e) => props.onManualDatabaseChange(e.target.value)}
                  placeholder="(default)"
                />
              </label>
              <label className="field">
                <span>SCHEMA</span>
                <input
                  type="text"
                  value={props.manualSchema}
                  onChange={(e) => props.onManualSchemaChange(e.target.value)}
                  placeholder="(default)"
                />
              </label>
            </div>
            <p className="caption">Leave both blank to use the Lambda's default target.</p>
          </>
        )}

        <label className="field">
          <span>Table hint (optional)</span>
          <input
            type="text"
            value={props.tableHint}
            onChange={(e) => props.onTableHintChange(e.target.value)}
            placeholder="EVENT__V, USER__SYS"
          />
        </label>
        <p className="caption">Comma-separated. Skips table selection and scopes the metadata query.</p>
      </section>

      <hr />

      <section>
        <h2 className="sidebar-heading">Behaviour</h2>
        <label className="field field--checkbox">
          <input
            type="checkbox"
            checked={props.showSqlExpandedByDefault}
            onChange={(e) => props.onShowSqlExpandedByDefaultChange(e.target.checked)}
          />
          <span>Expand SQL by default</span>
        </label>

        {props.sessionId ? (
          <p className="caption">
            Conversation memory: session <code>{props.sessionId.slice(0, 8)}...</code> — follow-ups refer back to
            this thread server-side. If the Lambda has no SESSION_TABLE configured, this is silently ignored.
          </p>
        ) : (
          <p className="caption">Conversation memory: no session yet — starts after your first question.</p>
        )}

        <button className="btn btn-secondary btn-block" onClick={props.onClearChat}>
          Clear chat
        </button>
      </section>
    </aside>
  );
}
