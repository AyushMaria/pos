import { useEffect, useRef } from "react";

/**
 * Global barcode capture — architecture §10.1.
 *
 * A USB HID wedge scanner types into the focused window very fast and ends
 * with Enter. A human types slowly. The difference is the only signal
 * available, so the rule is: a burst whose inter-character gaps are all under
 * `maxGapMs`, terminated by Enter, is a scan.
 *
 * v1 has no scanner — codes are typed — but this hook is what makes the
 * hardware a drop-in later, and it is worth having early for a second reason:
 * it listens globally, so a code entered while a dialog is open still lands in
 * the basket instead of into whatever field happens to have focus.
 *
 * The plan (§4) calls this out as the one piece worth isolating early, because
 * fast input, out-of-order key events and Enter-terminated codes are easy to
 * get subtly wrong and hard to debug at a counter.
 */
export interface BarcodeCaptureOptions {
  /** Longest gap between characters still considered machine-speed. */
  maxGapMs?: number;
  /** Shortest string worth treating as a code. */
  minLength?: number;
  /** Ignore capture while the user is typing in these elements. */
  respectFocus?: boolean;
  onScan: (code: string) => void;
}

const TYPING_TAGS = new Set(["INPUT", "TEXTAREA", "SELECT"]);

export function useBarcodeCapture({
  maxGapMs = 30,
  minLength = 4,
  respectFocus = true,
  onScan,
}: BarcodeCaptureOptions): void {
  // Refs, not state: a keystroke must not cause a re-render, and at scanner
  // speed a setState per character would drop characters on a slow machine.
  const buffer = useRef("");
  const lastKeyAt = useRef(0);
  const onScanRef = useRef(onScan);

  useEffect(() => {
    onScanRef.current = onScan;
  }, [onScan]);

  useEffect(() => {
    function handle(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;

      // Skip every text field, including the register's own barcode field.
      //
      // That field is a normal input: a scanner types into it and Enter
      // submits the form, which already routes digits to a lookup and text to
      // a search. Running this hook there as well would put every scan down
      // two paths at once — and the one that wins would depend on the order
      // preventDefault happens to fire in.
      //
      // So this hook covers exactly the case the field cannot: a scan while
      // focus is somewhere else, such as a tender dialog or the search
      // results list. That is the whole reason it is global.
      const typingHere =
        respectFocus && target != null && TYPING_TAGS.has(target.tagName);

      if (typingHere) return;

      const now = event.timeStamp || performance.now();
      const gap = now - lastKeyAt.current;
      lastKeyAt.current = now;

      if (event.key === "Enter") {
        const code = buffer.current;
        buffer.current = "";
        if (code.length >= minLength) {
          event.preventDefault();
          onScanRef.current(code);
        }
        return;
      }

      // A single printable character. Modifier combinations are shortcuts,
      // never barcode content.
      if (event.key.length !== 1 || event.ctrlKey || event.altKey || event.metaKey) {
        return;
      }

      // Too slow to be a machine: this is a person, so start a fresh buffer
      // rather than gluing their keystrokes onto a half-finished scan.
      if (gap > maxGapMs) buffer.current = "";

      buffer.current += event.key;
    }

    window.addEventListener("keydown", handle);
    return () => window.removeEventListener("keydown", handle);
  }, [maxGapMs, minLength, respectFocus]);
}
