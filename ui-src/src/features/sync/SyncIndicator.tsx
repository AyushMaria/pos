import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../../core/api/client";
import { sync } from "../../core/api/register";
import type { SessionResponse, SyncStatusResponse } from "../../core/api/contract";
import { PermissionGate } from "../../core/rbac/PermissionGate";

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
export function SyncIndicator({ session }: { session?: SessionResponse | null }) {
  const [status, setStatus] = useState<SyncStatusResponse | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [pushing, setPushing] = useState(false);
  const [retrying, setRetrying] = useState(false);

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
  // A quarantined sale is not in the backlog — it has been set aside, not
  // queued — so backlog alone said "Synced" while a sale sat in the cloud's
  // reject pile, with the only hint a sentence in a tooltip. The badge read
  // green and the two halves of that tooltip contradicted each other:
  // "Everything has been sent. 1 could not be sent and need a manager."
  const failures = status.failures ?? 0;
  const tone = status.needs_update || failures > 0
    ? "bad"
    : !status.online
      ? "warn"
      : waiting
        ? "busy"
        : "ok";

  async function retryFailures() {
    setRetrying(true);
    try {
      setStatus((await sync.retryFailures()).status);
    } catch {
      // The badge is about to be refreshed anyway, and a failed retry is
      // still a failure the manager can see — no separate complaint.
    } finally {
      setRetrying(false);
      void refresh();
    }
  }

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
      {/*
        Deliberately ungated, unlike "Try again" below it. Draining the queue
        early is the same act as waiting ninety seconds for the next cycle:
        the rows were written under permissions already checked, they push
        under the terminal's own credentials, and RLS still refuses each one
        on its own merits. Nothing is granted by asking sooner.

        And the person who has just plugged the cable back in is usually the
        cashier. Gating this would hide the button from exactly the people who
        want it, to prevent an act with no consequence. Recorded in phase 7
        slice 2 and asserted by `test_sync_push_is_deliberately_ungated`.
      */}
      {waiting && status.online && (
        <button type="button" className="link" disabled={pushing} onClick={() => void pushNow()}>
          {pushing ? "Sending…" : "Send now"}
        </button>
      )}
      {/*
        Only for someone who could read the failures list in the first place.
        This is UX, not security — the endpoint checks the same permission —
        but a cashier offered a button that will only refuse them learns
        nothing except that the till is broken.
      */}
      {failures > 0 && status.online && (
        <PermissionGate session={session ?? null} permission="report.sales.store">
          <button
            type="button"
            className="link"
            disabled={retrying}
            onClick={() => void retryFailures()}
          >
            {retrying ? "Trying…" : "Try again"}
          </button>
        </PermissionGate>
      )}
    </span>
  );
}

function summary(status: SyncStatusResponse): string {
  if (status.needs_update) return "Update needed";
  // Before the backlog, because a refused sale outranks a sent one: the day's
  // takings differ between this till and the cloud until somebody looks at it.
  // Claim 5 of the M2 test is that a refusal is *visible*, and a green badge
  // is not visible.
  const failures = status.failures ?? 0;
  if (failures > 0) {
    // Both words move: "1 sale needs", "2 sales need".
    return `${failures} ${failures === 1 ? "sale needs" : "sales need"} a manager`;
  }
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
  // Optional in the schema because it has a server-side default; the UI
  // still has to say what it means when it is absent, which is "none".
  const failures = status.failures ?? 0;

  const lines = [status.online ? "Connected." : "No connection to the cloud."];

  if (status.backlog > 0) {
    lines.push(`${status.backlog} sale(s) held on this terminal — nothing is lost.`);
  } else if (failures === 0) {
    lines.push("Everything has been sent.");
  } else {
    // Never "Everything has been sent." alongside a refusal. That sentence
    // and the next one used to appear together and flatly contradict.
    lines.push("Everything else has been sent.");
  }

  if (failures > 0) {
    lines.push(
      `${failures} sale(s) refused by the cloud, still on this terminal. ` +
        "A manager can see why in the failures list.",
    );
  }
  if (status.last_push_at) lines.push(`Last sent ${status.last_push_at}.`);
  if (status.last_error) lines.push(status.last_error);
  return lines.join(" ");
}
