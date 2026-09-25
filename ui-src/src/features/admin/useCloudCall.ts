import { useCallback, useState } from "react";
import { ApiError } from "../../core/api/client";

/**
 * Offline is a different sentence from refused, and a different next step.
 *
 * Shared by every admin tab that reads or writes the cloud directly: the
 * catalogue since phase 6, the owner's reports since phase 8.
 */
export function useCloudCall() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [offline, setOffline] = useState(false);

  const run = useCallback(async <T,>(work: () => Promise<T>): Promise<T | null> => {
    setBusy(true);
    setError(null);
    setOffline(false);
    try {
      return await work();
    } catch (cause) {
      if (cause instanceof ApiError && cause.isUnavailable) {
        setOffline(true);
      } else {
        setError(cause instanceof ApiError ? cause.message : "That did not work.");
      }
      return null;
    } finally {
      setBusy(false);
    }
  }, []);

  return { busy, error, offline, run, clearError: () => setError(null) };
}
