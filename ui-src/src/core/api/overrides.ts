import { request } from "./client";
import type { OverrideResponse, Permission } from "./contract";

/**
 * The supervisor override — architecture §11.3.
 *
 * One call, and the only one in the application that hands out a permission
 * rather than spending one. What comes back is a grant: the cashier's session
 * keeps every key it had and gains this one for ninety seconds.
 *
 * The status code carries as much as the message. 409 means the session
 * already holds the key and nothing needed authorising; 423 means the
 * approver is locked out and waiting is the only thing that helps; 503 means
 * this terminal has never seen them and cannot check a PIN it does not have.
 * A caller that renders `message` and ignores `status` will say the right
 * words and do the wrong thing.
 */
export const overrides = {
  authorize: (approverCode: string, pin: string, permission: Permission) =>
    request<OverrideResponse>("/overrides/authorize", {
      method: "POST",
      body: JSON.stringify({
        approver_code: approverCode,
        pin,
        permission,
      }),
    }),
};
