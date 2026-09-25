import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ROLE_PERMISSIONS,
  type Permission,
  type Role,
  type SessionResponse,
} from "../../core/api/contract";
import { ApiError } from "../../core/api/client";

/**
 * The owner's reports at the UI layer — phase 8 slice 4.
 *
 * The margin column's third enforcement point. The other two are
 * `tests/test_rls.py` (Postgres nulls cost for a caller without
 * `report.margin`) and `tests/test_permission_matrix.py` (the route withdraws
 * it again). This one only decides whether a supervisor is shown a Margin
 * heading over a column of blanks, which is the thing to get right here.
 */

const api = {
  sales: vi.fn(),
  products: vi.fn(),
  stock: vi.fn(),
  exportCsv: vi.fn(),
};

vi.mock("../../core/api/reports", () => ({
  reports: {
    sales: (...a: unknown[]) => api.sales(...a),
    products: (...a: unknown[]) => api.products(...a),
    stock: (...a: unknown[]) => api.stock(...a),
    exportCsv: (...a: unknown[]) => api.exportCsv(...a),
  },
}));

vi.mock("../../core/api/admin", () => ({
  admin: { searchProducts: vi.fn(), lowStock: vi.fn().mockResolvedValue({ rows: [] }) },
}));

vi.mock("../../core/api/register", () => ({
  catalog: { taxCodes: vi.fn().mockResolvedValue({ tax_codes: [] }) },
  sync: { failures: vi.fn().mockResolvedValue({ items: [] }), retryFailures: vi.fn() },
}));

const { ReportsTab, localToday } = await import("./ReportsTab");
const { AdminScreen } = await import("./AdminScreen");

const as = (role: Role): SessionResponse =>
  ({
    employee_code: "X001",
    full_name: `${role} Testperson`,
    permissions: [...ROLE_PERMISSIONS[role]] as Permission[],
  }) as SessionResponse;

const DAY = {
  day: "2026-09-24",
  sales_count: 10,
  takings: 412_300,
  cash: 300_000,
  upi_attested: 62_300,
  upi_verified: 50_000,
  tax: 21_000,
  discounts: 0,
  rounding: -170,
  under_review_count: 1,
  under_review_total: 3_740,
  cost: 350_000,
  margin: 52_300,
  uncosted_sales: 10_000,
};

const salesReport = (withMargin: boolean) => {
  const strip = <T extends object>(row: T): T =>
    withMargin ? row : { ...row, cost: null, margin: null, uncosted_sales: null };
  return {
    since: "2026-09-24",
    until: "2026-09-24",
    days: [strip(DAY)],
    total: strip({ ...DAY, day: null }),
    margin_visible: withMargin,
  };
};

const headers = () => screen.getAllByRole("columnheader").map((th) => th.textContent);

beforeEach(() => {
  vi.clearAllMocks();
  api.sales.mockResolvedValue(salesReport(true));
  api.products.mockResolvedValue({
    since: "2026-09-24",
    until: "2026-09-24",
    rows: [],
    total: { sales: 0, tax: 0, discounts: 0 },
    margin_visible: true,
  });
  api.stock.mockResolvedValue({
    rows: [
      {
        product_id: "p1", sku: "SKU-1001", name: "Aashirvaad Atta 5 kg", uom: "each",
        on_hand: 12_000, reorder_point: 4_000, price: 27_500, value_at_price: 330_000,
        unit_cost: null, value_at_cost: null,
      },
    ],
    total: { value_at_price: 330_000, value_at_cost: null },
    margin_visible: false,
  });
});

describe("who sees what", () => {
  it("is not a tab a cashier is offered", () => {
    render(<AdminScreen session={as("cashier")} onClose={() => {}} />);
    expect(screen.queryByRole("button", { name: "Reports" })).toBeNull();
  });

  it("is a tab a supervisor is offered", () => {
    render(<AdminScreen session={as("supervisor")} onClose={() => {}} />);
    expect(screen.getByRole("button", { name: "Reports" })).toBeTruthy();
  });

  it("shows a supervisor the takings and no cost or margin column", async () => {
    api.sales.mockResolvedValue(salesReport(false));
    render(<ReportsTab session={as("supervisor")} />);

    expect(await screen.findByText("4123.00", { selector: "tfoot td" })).toBeTruthy();
    expect(headers()).toContain("Takings");
    expect(headers()).not.toContain("Cost");
    expect(headers()).not.toContain("Margin");
  });

  it("shows a manager the same day with cost and margin", async () => {
    render(<ReportsTab session={as("manager")} />);

    expect(await screen.findByText("523.00", { selector: "tfoot td" })).toBeTruthy();
    expect(headers()).toEqual(expect.arrayContaining(["Cost", "Margin", "Sold with no cost"]));
    // The part of the day the margin does not cover, said in words.
    expect(screen.getByText(/100\.00 was sold\s+with no cost on file/)).toBeTruthy();
  });
});

describe("the range", () => {
  it("opens on today, in this machine's zone", async () => {
    render(<ReportsTab session={as("manager")} />);
    await waitFor(() => expect(api.sales).toHaveBeenCalled());

    const [range] = api.sales.mock.calls[0]!;
    expect(range.since).toBe(localToday());
    expect(range.until).toBe(localToday());
    expect(range.tz).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone);
  });

  it("reads again when the range changes", async () => {
    const user = userEvent.setup();
    render(<ReportsTab session={as("manager")} />);
    await waitFor(() => expect(api.sales).toHaveBeenCalledTimes(1));

    const from = screen.getByLabelText("From");
    await user.clear(from);
    await user.type(from, "2026-09-01");

    await waitFor(() =>
      expect(api.sales).toHaveBeenLastCalledWith(
        expect.objectContaining({ since: "2026-09-01" }),
      ),
    );
  });

  it("asks the stock report for no range at all", async () => {
    const user = userEvent.setup();
    render(<ReportsTab session={as("supervisor")} />);
    await user.click(screen.getByRole("button", { name: "Stock" }));

    expect(await screen.findByText("Aashirvaad Atta 5 kg")).toBeTruthy();
    expect(api.stock).toHaveBeenCalledWith();
    expect(screen.queryByLabelText("From")).toBeNull();
    expect(headers()).not.toContain("Unit cost");
  });
});

describe("offline, and the file", () => {
  it("says the reports need the internet rather than showing an empty day", async () => {
    api.sales.mockRejectedValue(new ApiError(503, "no internet"));
    render(<ReportsTab session={as("manager")} />);

    expect(await screen.findByText(/need the internet/)).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("exports the view on screen and says where the file went", async () => {
    const user = userEvent.setup();
    api.exportCsv.mockResolvedValue({
      path: "C:\\ProgramData\\POS\\reports\\sales-2026-09-24-to-2026-09-24-093000.csv",
      rows: 1,
    });
    render(<ReportsTab session={as("manager")} />);
    await screen.findByText("4123.00", { selector: "tfoot td" });

    await user.click(screen.getByRole("button", { name: "Export to CSV" }));

    expect(api.exportCsv).toHaveBeenCalledWith({
      report: "sales",
      since: localToday(),
      until: localToday(),
      tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
    });
    const saved = await screen.findByText(/Saved .*sales-2026-09-24/);
    expect(within(saved).getByText(/opens in Excel/)).toBeTruthy();
  });
});

describe("localToday", () => {
  it("is the local calendar day, not the UTC one", () => {
    // 00:05 on the 25th wherever this runs, which is the 24th in UTC east
    // of Greenwich — the case `toISOString().slice(0, 10)` gets wrong.
    expect(localToday(new Date(2026, 8, 25, 0, 5))).toBe("2026-09-25");
  });
});
