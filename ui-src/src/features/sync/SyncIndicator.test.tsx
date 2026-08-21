import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SyncStatusResponse } from "../../core/api/contract";

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
};

vi.mock("../../core/api/register", () => ({
  sync: {
    status: (...args: unknown[]) => api.status(...args),
    pushNow: (...args: unknown[]) => api.pushNow(...args),
    failures: (...args: unknown[]) => api.failures(...args),
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
