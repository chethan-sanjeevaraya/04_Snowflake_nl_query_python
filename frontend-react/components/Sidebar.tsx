interface Props {
  targets: string[];
  targetError: string;
  discovering: boolean;
  onDiscoverTargets: () => void;
  selectedTarget: string; // "" = Lambda default, else "Database / schema"
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
      <div className="brand"><span className="brand-mark" aria-hidden="true">✦</span><div>Snowflake<span className="brand-subtitle">DATA ASSISTANT</span></div></div>
      <button className="btn btn-primary btn-block new-chat" onClick={props.onClearChat}>+ New conversation</button>
      <section>
        <h2 className="sidebar-heading">Data workspace</h2>
        <button className="btn btn-secondary btn-block" onClick={props.onDiscoverTargets} disabled={props.discovering}>
          {props.discovering ? "Finding workspaces…" : "Browse workspaces"}
        </button>

        {props.targets.length > 0 ? (
          <label className="field">
            <span>Database / schema</span>
            <select value={props.selectedTarget} onChange={(e) => props.onSelectedTargetChange(e.target.value)}>
              <option value="">Default workspace</option>
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
                <span>Database</span>
                <input
                  type="text"
                  value={props.manualDatabase}
                  onChange={(e) => props.onManualDatabaseChange(e.target.value)}
                  placeholder="(default)"
                />
              </label>
              <label className="field">
                <span>Schema</span>
                <input
                  type="text"
                  value={props.manualSchema}
                  onChange={(e) => props.onManualSchemaChange(e.target.value)}
                  placeholder="(default)"
                />
              </label>
            </div>
            <p className="caption">Leave blank to use your default workspace.</p>
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
        <p className="caption">Focus your question on specific tables, separated by commas.</p>
      </section>

      <hr />

      <section>
        <h2 className="sidebar-heading">Preferences</h2>
        <label className="field field--checkbox">
          <input
            type="checkbox"
            checked={props.showSqlExpandedByDefault}
            onChange={(e) => props.onShowSqlExpandedByDefaultChange(e.target.checked)}
          />
          <span>Expand SQL by default</span>
        </label>

        <p className="caption">{props.sessionId ? "Continue exploring with follow-up questions." : "Your conversation starts with your first question."}</p>
      </section>
    </aside>
  );
}
