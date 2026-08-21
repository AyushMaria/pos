import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jest-dom's matchers, for assertions that read like the thing being checked:
// toHaveTextContent over a manual textContent comparison.
import "@testing-library/jest-dom/vitest";

// Testing Library auto-cleans only when Vitest globals are on, and they are
// deliberately off here. Without this, every render stays in the document and
// the second test in a file sees two registers.
afterEach(cleanup);
