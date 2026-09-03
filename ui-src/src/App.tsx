import { useEffect, useState } from "react";
import { ApiError, api } from "./core/api/client";
import type { HealthResponse, SessionResponse } from "./core/api/contract";
import { LoginScreen } from "./features/auth/LoginScreen";
import { RegisterScreen } from "./features/register/RegisterScreen";
import { AdminScreen } from "./features/admin/AdminScreen";
import { StockroomScreen } from "./features/stockroom/StockroomScreen";

export function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [session, setSession] = useState<SessionResponse | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  // Three screens, still no router. A till has one job and two back doors —
  // a pallet arriving and a catalogue to fix — and a string is cheaper than a
  // routing dependency for that. Admin is the only one of the three that
  // needs the internet, and it says so itself rather than being hidden here.
  const [screen, setScreen] = useState<"register" | "stockroom" | "admin">(
    "register",
  );

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

  if (!session) {
    return <LoginScreen health={health} onSignedIn={setSession} />;
  }

  if (screen === "stockroom") {
    return (
      <StockroomScreen session={session} onClose={() => setScreen("register")} />
    );
  }
  if (screen === "admin") {
    return <AdminScreen session={session} onClose={() => setScreen("register")} />;
  }
  return (
    <RegisterScreen
      session={session}
      onOpenStockroom={() => setScreen("stockroom")}
      onOpenAdmin={() => setScreen("admin")}
    />
  );
}
