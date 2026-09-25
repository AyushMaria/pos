import { request } from "./client";
import type {
  ProductReportResponse,
  ReportExportRequest,
  ReportExportResponse,
  SalesReportResponse,
  StockReportResponse,
} from "./contract";

/**
 * The owner's reports — phase 8 slice 4.
 *
 * Read from the cloud, so every call can fail with 503 the way admin calls
 * can. `since` and `until` are local days (`YYYY-MM-DD`), inclusive, and
 * `tz` is the zone they are local to; the cloud works out the instants,
 * which is the one place that can do it for every till at once.
 *
 * The cost columns come back null for a caller without `report.margin`. The
 * screen hides them too, but it is not what withholds them.
 */
export interface ReportRange {
  since: string;
  until: string;
  tz: string;
}

const range = ({ since, until, tz }: ReportRange) =>
  new URLSearchParams({ since, until, tz }).toString();

export const reports = {
  sales: (r: ReportRange) => request<SalesReportResponse>(`/reports/sales?${range(r)}`),

  products: (r: ReportRange) =>
    request<ProductReportResponse>(`/reports/products?${range(r)}`),

  stock: () => request<StockReportResponse>("/reports/stock"),

  /**
   * Written by the till, under its data directory, and the path returned.
   * The page cannot download a file inside the webview, and the export is
   * fetched again server-side so what it contains is the session's call.
   */
  exportCsv: (body: ReportExportRequest) =>
    request<ReportExportResponse>("/reports/export", {
      method: "POST",
      body: JSON.stringify(body),
    }),
};
