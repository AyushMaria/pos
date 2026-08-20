import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "../../core/api/client";
import type { HealthResponse, SessionResponse } from "../../core/api/contract";

/**
 * Sign-in at the till.
 *
 * Designed for the counter rather than for a desk: large targets, the PIN
 * field cleared and refocused after every failure, and no error that leaves
 * the cashier without a next action. The keyboard never has to leave the two
 * fields — a queue is not the place to reach for a mouse.
 */
export function LoginScreen({
  health,
  onSignedIn,
}: {
  health: HealthResponse | null;
  onSignedIn: (session: SessionResponse) => void;
}) {
  const [employeeCode, setEmployeeCode] = useState("");
  const [pin, setPin] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const pinRef = useRef<HTMLInputElement>(null);
  const codeRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    codeRef.current?.focus();
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await api.login({ employee_code: employeeCode.trim(), pin }));
    } catch (cause) {
      setError(
        cause instanceof ApiError ? cause.message : "Something went wrong.",
      );
      setPin("");
      pinRef.current?.focus();
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="card">
      <h1>Sign in</h1>
      <p className="sub">
        {health
          ? `${health.store_code} · ${health.terminal_code} · schema v${health.schema_version}` +
            (health.cloud_configured ? "" : " · offline mode")
          : " "}
      </p>

      <form onSubmit={submit} autoComplete="off">
        <label htmlFor="code">Employee code</label>
        <input
          id="code"
          ref={codeRef}
          value={employeeCode}
          onChange={(e) => setEmployeeCode(e.target.value.toUpperCase())}
          autoCapitalize="characters"
          required
        />

        <label htmlFor="pin">PIN</label>
        <input
          id="pin"
          ref={pinRef}
          type="password"
          inputMode="numeric"
          pattern="[0-9]*"
          value={pin}
          onChange={(e) => setPin(e.target.value)}
          required
        />

        <button type="submit" disabled={busy || !employeeCode || pin.length < 4}>
          {busy ? "Checking…" : "Sign in"}
        </button>
      </form>

      {error && (
        <p className="msg error" role="alert">
          {error}
        </p>
      )}
    </main>
  );
}
