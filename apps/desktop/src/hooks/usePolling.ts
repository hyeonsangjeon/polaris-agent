import { useCallback, useEffect, useRef, useState } from "react";

interface PollingState<T> {
  data: T | null;
  error: Error | null;
  loading: boolean;
  refresh: () => void;
}

export function usePolling<T>(
  task: () => Promise<T>,
  options: { enabled?: boolean; intervalMs?: number; cacheKey?: string } = {},
): PollingState<T> {
  const { enabled = true, intervalMs = 4000, cacheKey } = options;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [settledKey, setSettledKey] = useState<string | undefined>(cacheKey);
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
    setLoading(true);
    let alive = true;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const result = await taskRef.current();
        if (!alive) return;
        setData(result);
        setError(null);
        setSettledKey(cacheKey);
      } catch (reason) {
        if (!alive) return;
        setError(reason instanceof Error ? reason : new Error("Request failed"));
        setSettledKey(cacheKey);
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
  }, [cacheKey, enabled, intervalMs, refreshKey]);

  const current = settledKey === cacheKey;
  return {
    data: current ? data : null,
    error: current ? error : null,
    loading: loading || !current,
    refresh,
  };
}
