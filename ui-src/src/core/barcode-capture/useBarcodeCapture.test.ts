import { renderHook } from "@testing-library/react";
import { act } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useBarcodeCapture } from "./useBarcodeCapture";

/**
 * Execution plan §15: Vitest for the barcode capture timing hook.
 *
 * This is the piece the plan singles out as worth isolating early, because
 * fast input, out-of-order key events and Enter-terminated codes are easy to
 * get subtly wrong and impossible to debug at a busy counter. Every timing
 * rule below is asserted rather than assumed.
 */

let clock = 0;

function press(
  key: string,
  options: { gapMs?: number; target?: HTMLElement; ctrlKey?: boolean } = {},
) {
  clock += options.gapMs ?? 5;
  const event = new KeyboardEvent("keydown", {
    key,
    bubbles: true,
    cancelable: true,
    ctrlKey: options.ctrlKey ?? false,
  });
  // jsdom does not advance timeStamp, and the hook's whole job is timing.
  Object.defineProperty(event, "timeStamp", { value: clock });

  const target = options.target ?? document.body;
  act(() => {
    target.dispatchEvent(event);
  });
  return event;
}

function scan(code: string, gapMs = 5, target?: HTMLElement) {
  for (const character of code) press(character, { gapMs, target });
  return press("Enter", { gapMs, target });
}

beforeEach(() => {
  clock = 0;
  document.body.innerHTML = "";
});

describe("scanner input", () => {
  it("reports a fast burst terminated by Enter", () => {
    const onScan = vi.fn();
    renderHook(() => useBarcodeCapture({ onScan }));

    scan("8901262010016");

    expect(onScan).toHaveBeenCalledOnce();
    expect(onScan).toHaveBeenCalledWith("8901262010016");
  });

  it("reads consecutive scans without bleeding one into the next", () => {
    const onScan = vi.fn();
    renderHook(() => useBarcodeCapture({ onScan }));

    scan("8901262010016");
    scan("8901030865275");

    expect(onScan.mock.calls).toEqual([["8901262010016"], ["8901030865275"]]);
  });

  it("handles alphanumeric codes", () => {
    const onScan = vi.fn();
    renderHook(() => useBarcodeCapture({ onScan }));

    scan("ABC-123");

    expect(onScan).toHaveBeenCalledWith("ABC-123");
  });

  it("loses no characters in a long code", () => {
    const onScan = vi.fn();
    renderHook(() => useBarcodeCapture({ onScan }));

    scan("0108906088880911");

    expect(onScan).toHaveBeenCalledWith("0108906088880911");
  });
});

describe("human input is not a scan", () => {
  it("ignores slow typing", () => {
    const onScan = vi.fn();
    renderHook(() => useBarcodeCapture({ onScan }));

    // 120 ms between keystrokes: a person, not a machine.
    scan("colgate", 120);

    expect(onScan).not.toHaveBeenCalled();
  });

  it("does not glue slow keystrokes onto a half-finished scan", () => {
    const onScan = vi.fn();
    renderHook(() => useBarcodeCapture({ onScan }));

    press("8", { gapMs: 5 });
    press("9", { gapMs: 5 });
    press("0", { gapMs: 500 }); // the cashier looked away mid-scan
    press("1", { gapMs: 5 });
    press("Enter", { gapMs: 5 });

    // The buffer restarted at the long gap, leaving "01" — too short to be a
    // code, and far better refused than sent as a wrong one.
    expect(onScan).not.toHaveBeenCalled();
  });

  it("ignores a burst that is too short to be a code", () => {
    const onScan = vi.fn();
    renderHook(() => useBarcodeCapture({ onScan }));

    scan("12");

    expect(onScan).not.toHaveBeenCalled();
  });

  it("ignores keyboard shortcuts", () => {
    const onScan = vi.fn();
    renderHook(() => useBarcodeCapture({ onScan }));

    press("c", { ctrlKey: true });
    press("v", { ctrlKey: true });
    scan("8901");

    expect(onScan).toHaveBeenCalledWith("8901");
  });

  it("ignores navigation keys", () => {
    const onScan = vi.fn();
    renderHook(() => useBarcodeCapture({ onScan }));

    press("Tab");
    press("ArrowDown");
    press("Shift");
    press("Enter");

    expect(onScan).not.toHaveBeenCalled();
  });
});

describe("focus", () => {
  it("stays out of text fields, including the register's own", () => {
    // The register's barcode field is a normal input: it receives the scan
    // natively and its form decides what to do. Capturing here as well would
    // put every scan down two paths at once.
    const onScan = vi.fn();
    renderHook(() => useBarcodeCapture({ onScan }));

    const field = document.createElement("input");
    field.dataset.barcodeField = "true";
    document.body.append(field);

    scan("8901262010016", 5, field);

    expect(onScan).not.toHaveBeenCalled();
  });

  it("stays out of an ordinary text input", () => {
    const onScan = vi.fn();
    renderHook(() => useBarcodeCapture({ onScan }));

    const input = document.createElement("input");
    document.body.append(input);

    scan("8901262010016", 5, input);

    expect(onScan).not.toHaveBeenCalled();
  });

  it("captures a scan while a dialog has focus", () => {
    // The reason this hook is global at all: a code scanned with the tender
    // dialog open still reaches the basket.
    const onScan = vi.fn();
    renderHook(() => useBarcodeCapture({ onScan }));

    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    document.body.append(dialog);

    scan("8901262010016", 5, dialog);

    expect(onScan).toHaveBeenCalledWith("8901262010016");
  });

  it("can be told to capture everywhere", () => {
    const onScan = vi.fn();
    renderHook(() => useBarcodeCapture({ onScan, respectFocus: false }));

    const input = document.createElement("input");
    document.body.append(input);

    scan("8901262010016", 5, input);

    expect(onScan).toHaveBeenCalledWith("8901262010016");
  });
});

describe("wiring", () => {
  it("suppresses the default action only when it reports a scan", () => {
    const onScan = vi.fn();
    renderHook(() => useBarcodeCapture({ onScan }));

    for (const character of "8901") press(character);
    const enter = press("Enter");
    expect(enter.defaultPrevented).toBe(true);

    const bare = press("Enter");
    expect(bare.defaultPrevented).toBe(false);
  });

  it("uses the latest callback without re-registering the listener", () => {
    // A keystroke must never cause a re-render at scanner speed, so the
    // callback is held in a ref. It still has to be the current one.
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderHook(
      ({ onScan }) => useBarcodeCapture({ onScan }),
      { initialProps: { onScan: first } },
    );

    rerender({ onScan: second });
    scan("8901262010016");

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledOnce();
  });

  it("stops listening once unmounted", () => {
    const onScan = vi.fn();
    const { unmount } = renderHook(() => useBarcodeCapture({ onScan }));

    unmount();
    scan("8901262010016");

    expect(onScan).not.toHaveBeenCalled();
  });

  it("honours a custom gap threshold", () => {
    const onScan = vi.fn();
    renderHook(() => useBarcodeCapture({ onScan, maxGapMs: 150 }));

    // A slower scanner, or a machine on a loaded terminal.
    scan("8901262010016", 100);

    expect(onScan).toHaveBeenCalledWith("8901262010016");
  });
});
