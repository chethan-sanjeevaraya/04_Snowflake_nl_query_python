import { useEffect, useState } from "react";

/** Small persisted-state hook, used only for the handful of fields worth
 * surviving a page refresh (connection URL, session id, chat transcript).
 * The API key is deliberately NOT persisted this way -- see App.tsx -- so a
 * dev-testing key typed into the sidebar doesn't silently linger in the
 * browser's storage after the tab is closed. */
export function useLocalStorage<T>(
  key: string,
  initial: T
): [T, (v: T | ((prev: T) => T)) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw !== null ? (JSON.parse(raw) as T) : initial;
    } catch {
      return initial;
    }
  });

  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      // Storage can throw (private browsing, quota) -- losing persistence is
      // fine, losing the app isn't.
    }
  }, [key, value]);

  return [value, setValue];
}
