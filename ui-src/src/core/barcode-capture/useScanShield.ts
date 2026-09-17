import { useCallback, useRef } from "react";
import { MACHINE_GAP_MS } from "./useBarcodeCapture";

/**
 * Keeps a scanner out of a field that a person must type into.
 *
 * ## Why this is not the same as turning capture off
 *
 * `useBarcodeCapture` listens on `window` and already skips every `INPUT`,
 * so while a text field has focus the global hook is inert. Disabling it
 * stops a scan reaching the *basket*; it does nothing about a scan reaching
 * the *field*, because that is not the hook's doing. A wedge scanner is a
 * keyboard: the digits are ordinary keystrokes and the trailing Enter is an
 * ordinary submit.
 *
 * For the register's barcode field that is exactly right — a scan there is
 * the point. For a supervisor's PIN it is a problem with teeth. An item
 * scanned across the counter mid-authorisation types digits into the PIN box
 * and presses Enter, spending an attempt against that supervisor's PIN.
 * Attempts are now expensive, escalating and persisted, so a cashier could
 * lock their own supervisor out by accident, with the shop open, and the
 * audit row would say `pin.locked` and name the supervisor.
 *
 * ## The rule
 *
 * The same one the capture hook uses, and the constant is imported rather
 * than retyped so the two cannot drift: gaps under `MACHINE_GAP_MS` are a
 * machine. A person typing four digits does not hit 33 characters a second.
 *
 * The first character of a burst is indistinguishable from a keystroke — its
 * gap is however long the field sat idle — so it lands. The second one gives
 * it away, and from then on `onScanBlocked` fires and the trailing Enter is
 * swallowed rather than submitted.
 *
 * `onScanBlocked` is called once when the burst is recognised and again at
 * the Enter that ends it, because characters keep arriving in between. It
 * has to be idempotent: clear the field, say what happened, do not count
 * anything.
 */
export function useScanShield({
  maxGapMs = MACHINE_GAP_MS,
  onScanBlocked,
}: {
  maxGapMs?: number;
  onScanBlocked: () => void;
}): { onKeyDown: (event: { key: string; timeStamp: number; preventDefault: () => void }) => void } {
  const lastKeyAt = useRef(0);
  const burst = useRef(false);
  const blockedRef = useRef(onScanBlocked);
  blockedRef.current = onScanBlocked;

  const onKeyDown = useCallback(
    (event: { key: string; timeStamp: number; preventDefault: () => void }) => {
      const now = event.timeStamp || performance.now();
      const gap = now - lastKeyAt.current;
      lastKeyAt.current = now;

      if (event.key === "Enter") {
        if (burst.current) {
          // The Enter a scanner appends. Swallowing it is the whole point:
          // an attempt that is never submitted is an attempt that never
          // counts, which is what keeps a stray scan from locking somebody
          // out of their own shop.
          event.preventDefault();
          burst.current = false;
          blockedRef.current();
        }
        return;
      }

      if (event.key.length !== 1) return;

      if (gap <= maxGapMs) {
        burst.current = true;
        blockedRef.current();
      }
    },
    [maxGapMs],
  );

  return { onKeyDown };
}
