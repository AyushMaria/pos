import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MoneyOut, SessionResponse, ShiftFiguresOut } from "../../core/api/contract";

/**
 * The close screen — phase 8 slice 2b. What matters is the order: the count
 * is typed and committed before any expected figure is on screen.
 */

const money = (paise: number): MoneyOut => ({ paise, text: `₹${(paise / 100).toFixed(2)}` });

function figures(extra: Partial<ShiftFiguresOut> = {}): ShiftFiguresOut {
  return {
    cash_sales: money(3700),
    upi_attested: money(2000),
    upi_verified: money(0),
    cash_in: money(0),
    cash_out: money(0),
    rounding: money(-40),
    takings: money(5700),
    under_review_count: 1,
    under_review_total: money(900),
    sales_count: 3,
    expected_cash: null,
    counted_cash: null,
    variance: null,
    under_review_receipts: ["ST01-T1-000042"],
    ...extra,
  } as ShiftFiguresOut;
}

const x = vi.fn();
const close = vi.fn();
const writeZPdf = vi.fn();

vi.mock("../../core/api/register", () => ({
  shifts: {
    x: () => x(),
    close: (...args: unknown[]) => close(...args),
    writeZPdf: (...args: unknown[]) => writeZPdf(...args),
  },
}));

const { CloseShiftScreen } = await import("./CloseShiftScreen");

const session = { full_name: "Priya Nair", permissions: ["shift.close"] } as SessionResponse;
const person = () => userEvent.setup({ delay: 50 });

beforeEach(() => {
  x.mockReset().mockResolvedValue({ shift: { id: "s1" }, figures: figures() });
  close.mockReset().mockResolvedValue({
    close_id: "c1",
    shift: { id: "s1" },
    figures: figures({
      expected_cash: money(53700),
      counted_cash: money(53600),
      variance: money(-100),
    }),
    zreport_html: "<div class='zreport'>Z-report body</div>",
  });
  writeZPdf.mockReset().mockResolvedValue({ close_id: "c1", path: "C:/z/Z-T1.pdf" });
});

describe("closing the day", () => {
  it("shows no cash figure before the count", async () => {
    render(<CloseShiftScreen session={session} onClose={() => undefined} />);

    expect(await screen.findByText(/3 sales on this shift/i)).toBeVisible();
    expect(screen.getByText("ST01-T1-000042")).toBeVisible();
    expect(screen.queryByText(/expected/i)).toBeNull();
    expect(screen.queryByText("₹37.00")).toBeNull();
  });

  it("commits the count, then shows expected, variance and rounding apart", async () => {
    const user = person();
    render(<CloseShiftScreen session={session} onClose={() => undefined} />);
    await screen.findByText(/3 sales/i);

    await user.type(screen.getByLabelText(/cash counted/i), "536");
    await user.type(screen.getByLabelText(/note/i), "short a rupee");
    await user.click(screen.getByRole("button", { name: /close shift/i }));

    expect(close).toHaveBeenCalledWith(53600, "short a rupee");
    expect(await screen.findByText(/shift closed/i)).toBeVisible();
    expect(screen.getByText("₹537.00")).toBeVisible();
    expect(screen.getByText("₹-1.00")).toBeVisible();
    expect(screen.getByText("₹-0.40")).toBeVisible();
    expect(screen.getByText(/Z-report body/)).toBeVisible();
  });

  it("writes the PDF on demand", async () => {
    const user = person();
    render(<CloseShiftScreen session={session} onClose={() => undefined} />);
    await screen.findByText(/3 sales/i);
    await user.type(screen.getByLabelText(/cash counted/i), "536");
    await user.click(screen.getByRole("button", { name: /close shift/i }));

    await user.click(await screen.findByRole("button", { name: /save pdf/i }));

    expect(writeZPdf).toHaveBeenCalledWith("s1");
    expect(await screen.findByText(/C:\/z\/Z-T1.pdf/)).toBeVisible();
  });
});
