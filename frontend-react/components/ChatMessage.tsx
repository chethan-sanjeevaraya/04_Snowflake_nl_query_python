import type { ChatMessage as ChatMessageType } from "../types";
import { needsClarification } from "../types";
import { SuccessMessage } from "./SuccessMessage";
import { ClarificationMessage } from "./ClarificationMessage";
import { ErrorMessage } from "./ErrorMessage";

interface Props {
  message: ChatMessageType;
  index: number;
  isLatest: boolean;
  showSqlExpandedByDefault: boolean;
  onPickCandidate: (candidate: string) => void;
}

export function ChatMessage({ message, index, isLatest, showSqlExpandedByDefault, onPickCandidate }: Props) {
  if (message.role === "user") {
    return (
      <div className="msg-row msg-row--user">
        <div className="bubble bubble--user">{message.content}</div>
      </div>
    );
  }

  const { result } = message;
  let body: React.ReactNode;
  if (needsClarification(result)) {
    body = <ClarificationMessage result={result} isLatest={isLatest} onPick={onPickCandidate} />;
  } else if (result.ok) {
    body = <SuccessMessage result={result} msgIndex={index} showSqlExpandedByDefault={showSqlExpandedByDefault} />;
  } else {
    body = <ErrorMessage result={result} />;
  }

  return (
    <div className="msg-row msg-row--assistant">
      <div className="bubble bubble--assistant">{body}</div>
    </div>
  );
}
