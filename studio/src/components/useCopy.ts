import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Clipboard helper: returns [copied, copy]. `copied` flips to true for
 * `timeout` ms after a successful copy (for check-mark feedback).
 */
export function useCopy(timeout = 1500): [boolean, (text: string) => void] {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timer.current !== null) clearTimeout(timer.current);
    };
  }, []);

  const copy = useCallback(
    (text: string) => {
      void navigator.clipboard
        .writeText(text)
        .then(() => {
          setCopied(true);
          if (timer.current !== null) clearTimeout(timer.current);
          timer.current = setTimeout(() => setCopied(false), timeout);
        })
        .catch(() => {
          /* clipboard unavailable (insecure context) — ignore */
        });
    },
    [timeout],
  );

  return [copied, copy];
}
