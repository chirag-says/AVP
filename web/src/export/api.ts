// Export endpoints live on the scribe backend (port 7861), which has read
// access to both datasets. Read-only — it never writes the chatbot's data.
const API_BASE = import.meta.env.VITE_SCRIBE_API_BASE ?? "http://localhost:7861";

export type Dataset = "intake" | "consultations";
export type Format = "csv" | "xlsx";

export interface Filters {
  from?: string; // YYYY-MM-DD
  to?: string; // YYYY-MM-DD
  statuses: string[];
  ids: string[];
  flaggedOnly: boolean;
  search: string;
}

export interface IntakeRow {
  id: string;
  created_at: string;
  status: string;
  name: string | null;
  phone: string | null;
  flags: number;
}

export interface ConsultationRow {
  id: string;
  created_at: string;
  status: string;
  title: string;
  duration_s: number | null;
}

export type PickerRow = IntakeRow | ConsultationRow;

function toQuery(filters: Filters, extra: Record<string, string> = {}): string {
  const p = new URLSearchParams(extra);
  if (filters.from) p.set("from", filters.from);
  if (filters.to) p.set("to", filters.to);
  if (filters.statuses.length) p.set("status", filters.statuses.join(","));
  if (filters.ids.length) p.set("ids", filters.ids.join(","));
  if (filters.flaggedOnly) p.set("flagged", "1");
  if (filters.search.trim()) p.set("q", filters.search.trim());
  return p.toString();
}

export async function fetchList(
  dataset: Dataset,
  filters: Filters,
): Promise<{ rows: PickerRow[]; count: number }> {
  // The picker/count intentionally ignores the ids filter — it shows everything
  // matching the OTHER filters, so the user can then tick specific rows.
  const forList: Filters = { ...filters, ids: [] };
  const res = await fetch(`${API_BASE}/api/export/${dataset}/list?${toQuery(forList)}`);
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `Failed to load (${res.status})`);
  }
  return res.json();
}

/** Fetch the export as a blob and trigger a browser download with the server's filename. */
export async function downloadExport(
  dataset: Dataset,
  filters: Filters,
  format: Format,
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/export/${dataset}?${toQuery(filters, { format })}`);
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `Export failed (${res.status})`);
  }

  const disposition = res.headers.get("Content-Disposition") ?? "";
  const match = /filename="?([^"]+)"?/.exec(disposition);
  const filename = match?.[1] ?? `${dataset}_export.${format}`;

  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
