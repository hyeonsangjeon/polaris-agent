import { useCallback, useEffect, useRef, useState } from "react";

interface PollingState<T> {
  data: T | null;
  error: Error | null;
  loading: boolean;
  refresh: () => void;
}

export function usePolling<T>(
  task: () => Promise<T>,
  options: { enabled?: boolean; intervalMs?: number } = {},
): PollingState<T> {
  const { enabled = true, intervalMs = 4000 } = options;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(enabled);
  const taskRef = useRef(task);
  const refreshRef = useRef(0);
  const [refreshKey, setRefreshKey] = useState(0);
  taskRef.current = task;

  const refresh = useCallback(() => {
    refreshRef.current += 1;
    setRefreshKey(refreshRef.current);
  }, []);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    let alive = true;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const result = await taskRef.current();
        if (!alive) return;
        setData(result);
        setError(null);
      } catch (reason) {
        if (!alive) return;
        setError(reason instanceof Error ? reason : new Error("Request failed"));
      } finally {
        if (alive) {
          setLoading(false);
          timer = window.setTimeout(poll, Math.max(intervalMs, 1000));
        }
      }
    };
    void poll();
    return () => {
      alive = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [enabled, intervalMs, refreshKey]);

  return { data, error, loading, refresh };
}
