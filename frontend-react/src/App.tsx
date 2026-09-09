import { useRef, useState } from "react";
import { ask, discoverTargets, DEFAULT_TIMEOUT_SECONDS } from "./api";
import { useLocalStorage } from "./useLocalStorage";
import { Sidebar } from "./components/Sidebar";
import { ChatMessage } from "./components/ChatMessage";
import type { ChatMessage as ChatMessageType } from "./types";

function newId(): string {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

export default function App() {
  // Persisted across refreshes: connection URL, session, and the transcript.
  // The API key deliberately is NOT persisted -- see useLocalStorage.ts.
  const [apiUrl, setApiUrl] = useLocalStorage("snql.apiUrl", import.meta.env.VITE_API_URL ?? "");
  const [apiKey, setApiKey] = useState(import.meta.env.VITE_API_KEY ?? "");
  const [timeoutSeconds, setTimeoutSeconds] = useState(DEFAULT_TIMEOUT_SECONDS);
  const [sessionId, setSessionId] = useLocalStorage("snql.sessionId", "");
  const [messages, setMessages] = useLocalStorage<ChatMessageType[]>("snql.messages", []);

  const [targets, setTargets] = useState<string[]>([]);
  const [targetError, setTargetError] = useState("");
  const [discovering, setDiscovering] = useState(false);
  const [selectedTarget, setSelectedTarget] = useState("");
  const [manualDatabase, setManualDatabase] = useState("");
  const [manualSchema, setManualSchema] = useState("");

  const [tableHint, setTableHint] = useState("");
  const [showSqlExpandedByDefault, setShowSqlExpandedByDefault] = useState(false);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);

  const scrollRef = useRef<HTMLDivElement>(null);

  const database = targets.length > 0 ? selectedTarget.split(".")[0] ?? "" : manualDatabase;
  const schema = targets.length > 0 ? selectedTarget.split(".").slice(1).join(".") : manualSchema;

  function scrollToBottom() {
    requestAnimationFrame(() => {
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
    });
  }

  async function handleDiscoverTargets() {
    if (!apiUrl) {
      setTargetError("Set the API URL first.");
      setTargets([]);
      return;
    }
    setDiscovering(true);
    try {
      const { targets: found, error } = await discoverTargets(apiUrl, apiKey, timeoutSeconds);
      setTargets(found);
      setTargetError(error);
    } finally {
      setDiscovering(false);
    }
  }

  async function submit(query: string) {
    if (!apiUrl) {
      window.alert("Set the API Gateway invoke URL in the sidebar first.");
      return;
    }
    const userMsg: ChatMessageType = { role: "user", id: newId(), content: query };
    setMessages((prev) => [...prev, userMsg]);
    setSending(true);
    scrollToBottom();

    try {
      const result = await ask(apiUrl, apiKey, timeoutSeconds, query, {
        table: tableHint,
        database,
        schema,
        sessionId,
      });
      if (result.payload.session_id) {
        setSessionId(result.payload.session_id);
      }
      setMessages((prev) => [...prev, { role: "assistant", id: newId(), result }]);
    } finally {
      setSending(false);
      scrollToBottom();
    }
  }

  function handleSend() {
    const q = input.trim();
    if (!q || sending) return;
    setInput("");
    void submit(q);
  }

  function handlePickCandidate(candidate: string) {
    if (sending) return;
    void submit(candidate);
  }

  function handleClearChat() {
    setMessages([]);
    setSessionId("");
  }

  const lastAssistantIndex = (() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === "assistant") return i;
    }
    return -1;
  })();

  return (
    <div className="app-shell">
      <Sidebar
        apiUrl={apiUrl}
        onApiUrlChange={setApiUrl}
        apiKey={apiKey}
        onApiKeyChange={setApiKey}
        timeoutSeconds={timeoutSeconds}
        onTimeoutChange={setTimeoutSeconds}
        targets={targets}
        targetError={targetError}
        discovering={discovering}
        onDiscoverTargets={() => void handleDiscoverTargets()}
        selectedTarget={selectedTarget}
        onSelectedTargetChange={setSelectedTarget}
        manualDatabase={manualDatabase}
        onManualDatabaseChange={setManualDatabase}
        manualSchema={manualSchema}
        onManualSchemaChange={setManualSchema}
        tableHint={tableHint}
        onTableHintChange={setTableHint}
        showSqlExpandedByDefault={showSqlExpandedByDefault}
        onShowSqlExpandedByDefaultChange={setShowSqlExpandedByDefault}
        sessionId={sessionId}
        onClearChat={handleClearChat}
      />

      <main className="main">
        <header className="main-header">
          <h1>Snowflake NL Query</h1>
          <p className="caption">Ask a question in plain English; the Lambda picks the tables, writes the SQL, and runs it.</p>
        </header>

        <div className="chat-scroll" ref={scrollRef}>
          {!apiUrl && (
            <div className="info-banner">Set your API Gateway invoke URL in the sidebar to start.</div>
          )}
          {messages.map((m, i) => (
            <ChatMessage
              key={m.id}
              message={m}
              index={i}
              isLatest={i === lastAssistantIndex}
              showSqlExpandedByDefault={showSqlExpandedByDefault}
              onPickCandidate={handlePickCandidate}
            />
          ))}
          {sending && (
            <div className="msg-row msg-row--assistant">
              <div className="bubble bubble--assistant bubble--pending">
                <span className="spinner" /> Generating SQL and querying Snowflake…
              </div>
            </div>
          )}
        </div>

        <div className="chat-input-row">
          <input
            type="text"
            className="chat-input"
            placeholder="e.g. how many events were created last month?"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleSend();
            }}
            disabled={sending}
          />
          <button className="btn btn-primary" onClick={handleSend} disabled={sending || !input.trim()}>
            Send
          </button>
        </div>
      </main>
    </div>
  );
}
