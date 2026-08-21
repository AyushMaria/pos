import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../../core/api/client";
import { sync } from "../../core/api/register";
import type { SyncStatusResponse } from "../../core/api/contract";

/**
 * What the counter sees about the cloud — architecture §9, §4.
 *
 * One number matters to a cashier: how many sales are still on this machine.
 * Everything else here is for whoever they call when that number stops going
 * down.
 *
 * Deliberately small and deliberately not a dialog. Sync is never on the path
 * of a sale (§1.1), so it must never be able to interrupt one — a shop that
 * has lost its line keeps selling, and finds out from a badge rather than
 * from something it has to dismiss.
 */
export function SyncIndicator() {
  const [status, setStatus] = useState<SyncStatusResponse | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [pushing, setPushing] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setStatus(await sync.status());
    } catch (error) {
      // 503 means this terminal has no Supabase project — a normal
      // development setup, and not something to nag about.
      if (error instanceof ApiError && error.status === 503) setUnavailable(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  if (unavailable || !status) return null;

  const waiting = status.backlog > 0;
  const tone = status.needs_update
    ? "bad"
    : !status.online
      ? "warn"
      : waiting
        ? "busy"
        : "ok";

  async function pushNow() {
    setPushing(true);
    try {
      setStatus(await sync.pushNow());
    } catch {
      // The status refresh below reports whatever actually happened; a failed
      // manual push needs no separate complaint.
    } finally {
      setPushing(false);
      void refresh();
    }
  }

  return (
    <span className={`sync ${tone}`} title={detail(status)}>
      <span className="dot" aria-hidden="true" />
      <span className="label">{summary(status)}</span>
      {waiting && status.online && (
        <button type="button" className="link" disabled={pushing} onClick={() => void pushNow()}>
          {pushing ? "Sending…" : "Send now"}
        </button>
      )}
    </span>
  );
}

function summary(status: SyncStatusResponse): string {
  if (status.needs_update) return "Update needed";
  if (status.backlog === 0) return status.online ? "Synced" : "Offline";
  // The plural is worth getting right: this is the number a shopkeeper reads
  // when deciding whether to worry.
  const sales = `${status.backlog} sale${status.backlog === 1 ? "" : "s"}`;
  return status.online ? `Sending ${sales}` : `${sales} waiting`;
}

function detail(status: SyncStatusResponse): string {
  if (status.needs_update) {
    return "The server no longer accepts this version. Sales are safe on this machine, but they cannot be sent until the till is updated.";
  }
  const lines = [
    status.online ? "Connected." : "No connection to the cloud.",
    status.backlog > 0
      ? `${status.backlog} sale(s) held on this terminal — nothing is lost.`
      : "Everything has been sent.",
  ];
  // Optional in the schema because it has a server-side default; the UI
  // still has to say what it means when it is absent, which is "none".
  const failures = status.failures ?? 0;
  if (failures > 0) {
    lines.push(`${failures} could not be sent and need a manager.`);
  }
  if (status.last_push_at) lines.push(`Last sent ${status.last_push_at}.`);
  if (status.last_error) lines.push(status.last_error);
  return lines.join(" ");
}
