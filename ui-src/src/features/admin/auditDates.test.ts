import { describe, expect, it } from "vitest";
import { startOfLocalDay } from "./AdminScreen";

/**
 * The audit filter's dates — phase 7 slice 5.
 *
 * Found by reading an acceptance script before running it. The log's target
 * row happened at 18:35 UTC, which in IST is five minutes past midnight the
 * *next* morning. The table renders `toLocaleString()`, so the row shows on
 * the 16th; the filter sent the bare date, which Postgres reads as UTC
 * midnight, so it searched the 15th.
 *
 * The original was worse than off-by-one. `gte.<date>` and `lte.<date>` for a
 * single day is a window of zero width — midnight to midnight — so **every**
 * same-day filter returned nothing, on any date, in any timezone. A viewer
 * whose date filter can never match is worse than one with no filter at all,
 * because it answers.
 *
 * These assert the property rather than a timezone: a row you can *see* on a
 * day must be *found* by filtering that day, whatever the offset.
 */

const RANGE = (day: string) => ({
  since: new Date(startOfLocalDay(day)),
  until: new Date(startOfLocalDay(day, 1)),
});

const finds = (day: string, at: Date) => {
  const { since, until } = RANGE(day);
  return at >= since && at < until;
};

/** The real row from the pilot data: 2026-09-15T18:35:00Z. */
const TARGET = new Date("2026-09-15T18:35:00Z");

describe("filtering by the day a row appears to be on", () => {
  it("finds a row on the local day it renders as", () => {
    const shownOn = TARGET.toLocaleDateString("en-CA"); // YYYY-MM-DD, local
    expect(finds(shownOn, TARGET)).toBe(true);
  });

  it("does not find it on the day before the one it renders as", () => {
    const shownOn = new Date(TARGET);
    shownOn.setDate(shownOn.getDate() - 1);
    expect(finds(shownOn.toLocaleDateString("en-CA"), TARGET)).toBe(false);
  });

  it("covers a whole day, not an instant", () => {
    // The zero-width window that made every single-day filter empty.
    const { since, until } = RANGE("2026-09-15");
    expect(until.getTime() - since.getTime()).toBe(24 * 60 * 60 * 1000);
  });

  it("is exclusive at the top, so two adjacent days do not overlap", () => {
    // Otherwise a row at exactly midnight appears under both days, and a
    // count taken two ways disagrees with itself.
    const first = RANGE("2026-09-15");
    const second = RANGE("2026-09-16");
    expect(first.until.getTime()).toBe(second.since.getTime());
    expect(finds("2026-09-15", second.since)).toBe(false);
    expect(finds("2026-09-16", second.since)).toBe(true);
  });
});
