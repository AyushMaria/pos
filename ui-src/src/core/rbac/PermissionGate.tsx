import type { ReactNode } from "react";
import type { Permission, SessionResponse } from "../api/contract";

/**
 * Hides a control the current session may not use.
 *
 * This is UX, not security. It exists so a cashier is not shown a button that
 * will only refuse them, and so the screen matches what the person in front of
 * it can actually do. The FastAPI dependency rejects the request and Postgres
 * RLS rejects the sync; only the last of those is a control (architecture
 * §11.1).
 */
export function PermissionGate({
  session,
  permission,
  children,
  fallback = null,
}: {
  session: SessionResponse | null;
  permission: Permission;
  children: ReactNode;
  fallback?: ReactNode;
}) {
  const allowed = session?.permissions.includes(permission) ?? false;
  return <>{allowed ? children : fallback}</>;
}

export function useHasPermission(session: SessionResponse | null) {
  return (permission: Permission): boolean =>
    session?.permissions.includes(permission) ?? false;
}
