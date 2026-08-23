import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionResponse, SyncStatusResponse } from "../../core/api/contract";

/**
 * The sync badge — architecture §9.
 *
 * The thing being tested is mostly wording, and that is the point: this is
 * the only place a cashier learns that sales are stacking up on the machine,
 * and "3 sales waiting" and "Offline" call for different reactions from the
 * person reading them.
 */

const api = {
  status: vi.fn(),
  pushNow: vi.fn(),
  failures: vi.fn(),
  retryFailures: vi.fn(),
};

vi.mock("../../core/api/register", () => ({
  sync: {
    status: (...args: unknown[]) => api.status(...args),
    pushNow: (...args: unknown[]) => api.pushNow(...args),
    failures: (...args: unknown[]) => api.failures(...args),
    retryFailures: (...args: unknown[]) => api.retryFailures(...args),
  },
}));

const { SyncIndicator } = await import("./SyncIndicator");
const { ApiError } = await import("../../core/api/client");

function status(overrides: Partial<SyncStatusResponse> = {}): SyncStatusResponse {
  return {
    online: true,
    backlog: 0,
    oldest_pending_at: null,
    failures: 0,
    last_push_at: null,
    last_pull_at: null,
    last_error: null,
    needs_update: false,
    ...overrides,
  };
}

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
});

describe("what the counter is told", () => {
  it("says nothing much when everything has gone", async () => {
    api.status.mockResolvedValue(status());
    render(<SyncIndicator />);

    expect(await screen.findByText("Synced")).toBeDefined();
  });

  it("counts the sales still on this machine", async () => {
    api.status.mockResolvedValue(status({ online: false, backlog: 3 }));
    render(<SyncIndicator />);

    expect(await screen.findByText("3 sales waiting")).toBeDefined();
  });

  it("gets the singular right", async () => {
    // Pedantic, and worth it: this is the line a shopkeeper reads when
    // deciding whether something is wrong.
    api.status.mockResolvedValue(status({ online: false, backlog: 1 }));
    render(<SyncIndicator />);

    expect(await screen.findByText("1 sale waiting")).toBeDefined();
  });

  it("distinguishes sending from waiting", async () => {
    api.status.mockResolvedValue(status({ online: true, backlog: 2 }));
    render(<SyncIndicator />);

    expect(await screen.findByText("Sending 2 sales")).toBeDefined();
  });

  it("says when the till itself is the problem", async () => {
    // The one sync state that waiting will not fix (§17).
    api.status.mockResolvedValue(status({ needs_update: true, backlog: 5 }));
    render(<SyncIndicator />);

    expect(await screen.findByText("Update needed")).toBeDefined();
  });

  it("reassures that nothing is lost", async () => {
    api.status.mockResolvedValue(status({ online: false, backlog: 4 }));
    render(<SyncIndicator />);

    const badge = await screen.findByTitle(/nothing is lost/i);
    expect(badge).toBeDefined();
  });

  it("does not say Synced while a sale has been refused", async () => {
    // A quarantined sale is not in the backlog — it is set aside, not queued —
    // so the badge read "Synced" with a refused sale in the cloud's reject
    // pile, and the tooltip contradicted itself: "Everything has been sent.
    // 1 could not be sent and need a manager."
    //
    // M2 claim 5 is that a refusal is visible. A green badge is not.
    api.status.mockResolvedValue(status({ backlog: 0, failures: 1 }));
    render(<SyncIndicator />);

    expect(await screen.findByText("1 sale needs a manager")).toBeDefined();
    expect(screen.queryByText("Synced")).toBeNull();
  });

  it("does not claim everything was sent when something was not", async () => {
    api.status.mockResolvedValue(status({ backlog: 0, failures: 2 }));
    render(<SyncIndicator />);

    const badge = await screen.findByText("2 sales need a manager");
    const title = badge.closest("span[title]")?.getAttribute("title") ?? "";
    expect(title).not.toMatch(/^.*\bEverything has been sent\./);
    expect(title).toMatch(/refused by the cloud/i);
  });

  it("shows a refusal even while more sales are queued behind it", async () => {
    api.status.mockResolvedValue(status({ backlog: 3, failures: 1 }));
    render(<SyncIndicator />);

    expect(await screen.findByText("1 sale needs a manager")).toBeDefined();
  });
});

describe("sending now", () => {
  it("is not offered when there is nothing to send", async () => {
    api.status.mockResolvedValue(status({ online: true, backlog: 0 }));
    render(<SyncIndicator />);

    await screen.findByText("Synced");

    expect(screen.queryByRole("button", { name: /send now/i })).toBeNull();
  });

  it("is not offered while offline", async () => {
    // Pressing it could only fail, and a button that does nothing is worse
    // than no button when someone is trying to work out what is wrong.
    api.status.mockResolvedValue(status({ online: false, backlog: 2 }));
    render(<SyncIndicator />);

    await screen.findByText("2 sales waiting");

    expect(screen.queryByRole("button", { name: /send now/i })).toBeNull();
  });

  it("drains on request", async () => {
    const user = userEvent.setup();
    api.status.mockResolvedValue(status({ online: true, backlog: 2 }));
    api.pushNow.mockResolvedValue(status({ online: true, backlog: 0 }));
    render(<SyncIndicator />);

    await user.click(await screen.findByRole("button", { name: /send now/i }));

    await waitFor(() => expect(api.pushNow).toHaveBeenCalled());
  });

  it("stays quiet when a manual send fails", async () => {
    // The badge already reports the state; a failed press needs no separate
    // complaint, and a dialog here would interrupt a sale.
    const user = userEvent.setup();
    api.status.mockResolvedValue(status({ online: true, backlog: 2 }));
    api.pushNow.mockRejectedValue(new ApiError(500, "boom"));
    render(<SyncIndicator />);

    await user.click(await screen.findByRole("button", { name: /send now/i }));

    await waitFor(() => expect(api.pushNow).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("retrying what was set aside", () => {
  function aSession(permissions: string[]): SessionResponse {
    return {
      user_id: "018f0000-0000-7000-8000-000000000003",
      employee_code: "M001",
      full_name: "Priya Nair",
      store_id: "018f0000-0000-7000-8000-000000000100",
      roles: ["manager"],
      permissions,
      authenticated_at: "2026-08-23T11:00:00+00:00",
      offline: false,
    } as SessionResponse;
  }

  it("offers a manager a way out", async () => {
    api.status.mockResolvedValue(status({ backlog: 0, failures: 1 }));
    render(<SyncIndicator session={aSession(["report.sales.store"])} />);

    expect(await screen.findByRole("button", { name: "Try again" })).toBeDefined();
  });

  it("does not offer it to a cashier", async () => {
    // The endpoint refuses them anyway; a button that only ever says no
    // teaches a cashier that the till is broken.
    api.status.mockResolvedValue(status({ backlog: 0, failures: 1 }));
    render(<SyncIndicator session={aSession(["sale.create"])} />);

    await screen.findByText("1 sale needs a manager");
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("is not offered when nothing was set aside", async () => {
    api.status.mockResolvedValue(status({ backlog: 2, failures: 0 }));
    render(<SyncIndicator session={aSession(["report.sales.store"])} />);

    await screen.findByText("Sending 2 sales");
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("re-queues and shows what came back", async () => {
    api.status.mockResolvedValue(status({ backlog: 0, failures: 1 }));
    api.retryFailures.mockImplementation(async () => {
      // The retry cleared it server-side, so /sync/status has moved on too.
      // Leaving the old mock in place makes the fake contradict itself: the
      // component re-reads the status after every action, which is exactly
      // what it should do, and would read a failure that no longer exists.
      api.status.mockResolvedValue(status({ backlog: 0, failures: 0 }));
      return { requeued: 1, status: status({ backlog: 0, failures: 0 }) };
    });
    render(<SyncIndicator session={aSession(["report.sales.store"])} />);

    await userEvent.click(await screen.findByRole("button", { name: "Try again" }));

    expect(api.retryFailures).toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByText("Synced")).not.toBeNull());
  });

  it("stays quiet when the retry itself fails", async () => {
    // A sale still being refused is not a reason to interrupt the counter.
    api.status.mockResolvedValue(status({ backlog: 0, failures: 1 }));
    api.retryFailures.mockRejectedValue(new ApiError(403, "permission_denied"));
    render(<SyncIndicator session={aSession(["report.sales.store"])} />);

    await userEvent.click(await screen.findByRole("button", { name: "Try again" }));

    await waitFor(() =>
      expect(screen.queryByText("1 sale needs a manager")).not.toBeNull(),
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("a terminal with no cloud", () => {
  it("shows nothing at all", async () => {
    // A development setup with no Supabase project is normal, and a red
    // badge complaining about it every day trains people to ignore badges.
    api.status.mockRejectedValue(new ApiError(503, "no Supabase project"));
    const { container } = render(<SyncIndicator />);

    await waitFor(() => expect(api.status).toHaveBeenCalled());
    expect(container.querySelector(".sync")).toBeNull();
  });
});
