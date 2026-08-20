import { useEffect, useState } from "react";
import { ApiError, api } from "./core/api/client";
import type { HealthResponse, SessionResponse } from "./core/api/contract";
import { LoginScreen } from "./features/auth/LoginScreen";
import { RegisterScreen } from "./features/register/RegisterScreen";

export function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [session, setSession] = useState<SessionResponse | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);

  useEffect(() => {
    api
      .health()
      .then(setHealth)
      .catch((cause: unknown) =>
        setFatal(
          cause instanceof ApiError
            ? cause.message
            : "The till service is not responding.",
        ),
      );

    // A reload mid-shift must not sign the cashier out: the session lives in
    // the Python service, not in the page.
    api.session().then(setSession).catch(() => undefined);
  }, []);

  if (fatal) {
    return (
      <main className="card">
        <h1>The till could not start</h1>
        <p className="msg error">{fatal}</p>
        <p className="note muted">
          Close the window and open the till again. If it keeps happening, the
          log file is in the RetailPOS data folder.
        </p>
      </main>
    );
  }

  return session ? (
    <RegisterScreen session={session} />
  ) : (
    <LoginScreen health={health} onSignedIn={setSession} />
  );
}
