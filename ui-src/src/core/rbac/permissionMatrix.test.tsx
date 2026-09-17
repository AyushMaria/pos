import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  PERMISSIONS,
  ROLE_PERMISSIONS,
  type Permission,
  type Role,
  type SessionResponse,
} from "../api/contract";
import { PermissionGate, useHasPermission } from "./PermissionGate";

/**
 * The permission matrix at the UI layer — phase 7 slice 1.
 *
 * The other two layers are asserted in `tests/test_permission_matrix.py`
 * (Python and FastAPI) and `tests/test_rls.py` (the boundary). This is the
 * layer that is not security at all: it exists so a cashier is not shown a
 * button that will only refuse them.
 *
 * Which makes it worth saying what a failure here means. A gate that lets a
 * control through for the wrong role is not a vulnerability — FastAPI will
 * return 403 and Postgres will refuse the row. It is the thing phase 6 kept
 * producing instead: a screen that offers an action it cannot perform, so the
 * person at the counter finds out by being refused in front of a customer.
 *
 * `ROLE_PERMISSIONS` is generated from `app/domain/permissions.py`, so this
 * file asserts against the same table as the other two layers rather than a
 * fourth copy of §11.1.
 */

const ROLES = Object.keys(ROLE_PERMISSIONS) as Role[];

function sessionFor(role: Role): SessionResponse {
  return {
    user_id: `user-${role}`,
    employee_code: role.slice(0, 1).toUpperCase() + "001",
    full_name: `${role} Testperson`,
    store_id: "018f0000-0000-7000-8000-000000000100",
    roles: [role],
    permissions: [...ROLE_PERMISSIONS[role]],
    offline: false,
    authenticated_at: "2026-09-17T06:30:00+00:00",
  } as SessionResponse;
}

describe("the matrix, at the UI layer", () => {
  for (const role of ROLES) {
    for (const permission of PERMISSIONS) {
      const held = ROLE_PERMISSIONS[role].includes(permission);

      it(`${role} ${held ? "sees" : "does not see"} a ${permission} control`, () => {
        render(
          <PermissionGate session={sessionFor(role)} permission={permission}>
            <button>do the thing</button>
          </PermissionGate>,
        );

        const control = screen.queryByRole("button", { name: "do the thing" });
        if (held) {
          expect(control, `${role} holds ${permission} but the control is hidden`)
            .not.toBeNull();
        } else {
          expect(control, `${role} does not hold ${permission} but the control is shown`)
            .toBeNull();
        }
      });
    }
  }
});

describe("the gate itself", () => {
  it("shows nothing at all when there is no session", () => {
    // The splash renders before anyone has signed in. A gate that treated a
    // null session as permissive would flash every privileged control on the
    // way to the login screen.
    render(
      <PermissionGate session={null} permission="sale.create">
        <button>do the thing</button>
      </PermissionGate>,
    );
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("renders the fallback in place of a control it hides", () => {
    render(
      <PermissionGate
        session={sessionFor("cashier")}
        permission="report.margin"
        fallback={<span>Ask a manager</span>}
      >
        <button>Show margin</button>
      </PermissionGate>,
    );
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("Ask a manager")).toBeTruthy();
  });

  it("agrees with useHasPermission for every key and role", () => {
    // Two ways to ask the same question, and slice 2 replaces seven ad-hoc
    // checks with one or the other. They must not disagree.
    for (const role of ROLES) {
      const session = sessionFor(role);
      const has = useHasPermission(session);
      for (const permission of PERMISSIONS) {
        expect(has(permission), `${role} / ${permission}`).toBe(
          ROLE_PERMISSIONS[role].includes(permission),
        );
      }
    }
  });

  it("refuses a permission the session does not carry, even an unknown one", () => {
    const has = useHasPermission(sessionFor("admin"));
    expect(has("sale.teleport" as Permission)).toBe(false);
  });
});

describe("the matrix the UI is asserted against", () => {
  it("carries all five roles and all twenty keys", () => {
    // Guards against the generator emitting an empty table, which would make
    // every assertion above pass by iterating over nothing.
    expect(ROLES).toHaveLength(5);
    expect(PERMISSIONS).toHaveLength(20);
    for (const role of ROLES) {
      expect(ROLE_PERMISSIONS[role].length).toBeGreaterThan(0);
    }
  });

  it("gives no role a key that is not in PERMISSIONS", () => {
    for (const role of ROLES) {
      for (const permission of ROLE_PERMISSIONS[role]) {
        expect(PERMISSIONS).toContain(permission);
      }
    }
  });

  it("keeps the rule phase 1 had to prove end to end", () => {
    expect(ROLE_PERMISSIONS.cashier).not.toContain("report.margin");
  });
});
