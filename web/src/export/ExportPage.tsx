import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Database,
  FileSpreadsheet,
  FileText,
  Loader2,
  Search,
  Stethoscope,
  TriangleAlert,
} from "lucide-react";
import {
  downloadExport,
  fetchList,
  type ConsultationRow,
  type Dataset,
  type Filters,
  type Format,
  type IntakeRow,
  type PickerRow,
} from "./api";

const STATUS_OPTIONS: Record<Dataset, string[]> = {
  intake: ["completed", "in_progress", "abandoned"],
  consultations: ["summarized", "failed"],
};

const EMPTY_FILTERS: Filters = {
  from: "",
  to: "",
  statuses: [],
  ids: [],
  flaggedOnly: false,
  search: "",
};

function localDate(offsetDays = 0): string {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate(),
  ).padStart(2, "0")}`;
}

function isIntakeRow(row: PickerRow): row is IntakeRow {
  return "name" in row;
}

export default function ExportPage() {
  const [dataset, setDataset] = useState<Dataset>("intake");
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const [rows, setRows] = useState<PickerRow[]>([]);
  const [count, setCount] = useState(0);
  const [loading, setLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);

  const [downloading, setDownloading] = useState<Format | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  const patch = useCallback((next: Partial<Filters>) => {
    setFilters((prev) => ({ ...prev, ...next }));
  }, []);

  // Switching dataset resets the filters/selection that don't carry over
  // (statuses and picked rows are dataset-specific).
  const switchDataset = useCallback((next: Dataset) => {
    setDataset(next);
    setFilters((prev) => ({ ...prev, statuses: [], flaggedOnly: false, ids: [] }));
    setSelected(new Set());
  }, []);

  // Reload the picker whenever the dataset or a non-id filter changes.
  // Debounced so typing in search doesn't fire a request per keystroke.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setListError(null);
    const id = setTimeout(() => {
      fetchList(dataset, filters)
        .then((res) => {
          if (cancelled) return;
          setRows(res.rows);
          setCount(res.count);
          // Drop any picked ids that no longer match the filters.
          setSelected((prev) => {
            const visible = new Set(res.rows.map((r) => r.id));
            const next = new Set([...prev].filter((x) => visible.has(x)));
            return next.size === prev.size ? prev : next;
          });
        })
        .catch((e) => {
          if (!cancelled) setListError(e instanceof Error ? e.message : String(e));
        })
        .finally(() => !cancelled && setLoading(false));
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataset, filters.from, filters.to, filters.statuses.join(","), filters.flaggedOnly, filters.search]);

  const download = useCallback(
    async (format: Format) => {
      setDownloading(format);
      setDownloadError(null);
      try {
        await downloadExport(dataset, { ...filters, ids: [...selected] }, format);
      } catch (e) {
        setDownloadError(e instanceof Error ? e.message : String(e));
      } finally {
        setDownloading(null);
      }
    },
    [dataset, filters, selected],
  );

  const toggleStatus = (status: string) => {
    patch({
      statuses: filters.statuses.includes(status)
        ? filters.statuses.filter((s) => s !== status)
        : [...filters.statuses, status],
    });
  };

  const toggleRow = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const allVisibleSelected = rows.length > 0 && rows.every((r) => selected.has(r.id));
  const exportCount = selected.size || count;

  const preset = (label: string, from: string, to: string) => (
    <button
      key={label}
      onClick={() => patch({ from, to })}
      className={`press rounded-full border px-3 py-1 text-xs font-medium ${
        filters.from === from && filters.to === to
          ? "border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]"
          : "border-border bg-card/60 text-muted-foreground hover:text-foreground"
      }`}
    >
      {label}
    </button>
  );

  const activeStatuses = useMemo(() => STATUS_OPTIONS[dataset], [dataset]);

  return (
    <div className="scribe bg-background text-foreground">
      <div className="scribe-ambient" aria-hidden />

      <header className="scribe-header">
        <div className="mx-auto flex h-14 max-w-3xl items-center gap-3 px-5">
          <Database className="size-5 shrink-0 text-[var(--accent)]" />
          <span className="font-semibold tracking-tight">Export data</span>
          <div className="ml-auto flex items-center gap-2">
            <a
              href="/"
              className="press inline-flex items-center gap-1.5 rounded-full border border-border bg-card/60 px-3 py-1.5 text-xs font-medium text-muted-foreground hover:text-foreground"
            >
              <Stethoscope className="size-3.5" /> Reception
            </a>
            <a
              href="/consultation.html"
              className="press inline-flex items-center gap-1.5 rounded-full border border-border bg-card/60 px-3 py-1.5 text-xs font-medium text-muted-foreground hover:text-foreground"
            >
              Scribe
            </a>
          </div>
        </div>
      </header>

      <main className="view-enter mx-auto max-w-3xl space-y-6 px-5 py-8 sm:py-12">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">Export patient data</h1>
          <p className="text-sm text-muted-foreground">
            Download intake forms and consultation summaries as CSV or Excel. Narrow it with the
            filters below, or pick specific records.
          </p>
        </div>

        {/* Dataset segmented control */}
        <div className="inline-flex rounded-full border border-border bg-card/60 p-1 text-sm">
          {(["intake", "consultations"] as Dataset[]).map((ds) => (
            <button
              key={ds}
              onClick={() => switchDataset(ds)}
              className={`press rounded-full px-4 py-1.5 font-medium ${
                dataset === ds ? "bg-[var(--accent)] text-white shadow-sm" : "text-muted-foreground"
              }`}
            >
              {ds === "intake" ? "Intake forms" : "Consultation notes"}
            </button>
          ))}
        </div>

        {/* Filters */}
        <div className="space-y-5 rounded-2xl border border-border bg-card/50 p-5">
          <div className="space-y-2">
            <label className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Date range
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <input
                type="date"
                value={filters.from}
                max={filters.to || undefined}
                onChange={(e) => patch({ from: e.target.value })}
                className="rounded-lg border border-border bg-background px-3 py-1.5 text-sm"
              />
              <span className="text-muted-foreground">to</span>
              <input
                type="date"
                value={filters.to}
                min={filters.from || undefined}
                onChange={(e) => patch({ to: e.target.value })}
                className="rounded-lg border border-border bg-background px-3 py-1.5 text-sm"
              />
              <div className="flex flex-wrap gap-1.5">
                {preset("All", "", "")}
                {preset("Today", localDate(0), localDate(0))}
                {preset("7 days", localDate(-6), localDate(0))}
                {preset("30 days", localDate(-29), localDate(0))}
              </div>
            </div>
          </div>

          <div className="space-y-2">
            <label className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Status
            </label>
            <div className="flex flex-wrap gap-1.5">
              {activeStatuses.map((status) => (
                <button
                  key={status}
                  onClick={() => toggleStatus(status)}
                  className={`press rounded-full border px-3 py-1 text-xs font-medium capitalize ${
                    filters.statuses.includes(status)
                      ? "border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]"
                      : "border-border bg-card/60 text-muted-foreground hover:text-foreground"
                  }`}
                >
                  {status.replace("_", " ")}
                </button>
              ))}
              {dataset === "intake" && (
                <button
                  onClick={() => patch({ flaggedOnly: !filters.flaggedOnly })}
                  className={`press inline-flex items-center gap-1 rounded-full border px-3 py-1 text-xs font-medium ${
                    filters.flaggedOnly
                      ? "border-[var(--live)] bg-[var(--live)]/10 text-[var(--live)]"
                      : "border-border bg-card/60 text-muted-foreground hover:text-foreground"
                  }`}
                >
                  <TriangleAlert className="size-3" /> Flagged only
                </button>
              )}
            </div>
          </div>

          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <input
              value={filters.search}
              onChange={(e) => patch({ search: e.target.value })}
              placeholder={dataset === "intake" ? "Search name or phone" : "Search complaint or summary"}
              className="w-full rounded-lg border border-border bg-background py-2 pl-9 pr-3 text-sm"
            />
          </div>
        </div>

        {/* Record picker */}
        <div className="rounded-2xl border border-border bg-card/50">
          <div className="flex items-center justify-between border-b border-border px-4 py-3">
            <div className="flex items-center gap-2 text-sm">
              {loading ? (
                <span className="flex items-center gap-1.5 text-muted-foreground">
                  <Loader2 className="size-3.5 animate-spin" /> Loading…
                </span>
              ) : (
                <span>
                  <span className="font-semibold">{count}</span>{" "}
                  <span className="text-muted-foreground">record{count === 1 ? "" : "s"} match</span>
                </span>
              )}
            </div>
            {rows.length > 0 && (
              <button
                onClick={() =>
                  setSelected(allVisibleSelected ? new Set() : new Set(rows.map((r) => r.id)))
                }
                className="press text-xs font-medium text-[var(--accent)]"
              >
                {allVisibleSelected ? "Clear selection" : "Select all"}
              </button>
            )}
          </div>

          <div className="max-h-[38vh] overflow-y-auto">
            {listError ? (
              <p className="px-4 py-8 text-center text-sm text-destructive">{listError}</p>
            ) : rows.length === 0 && !loading ? (
              <p className="px-4 py-10 text-center text-sm text-muted-foreground">
                No records match these filters.
              </p>
            ) : (
              <ul className="divide-y divide-border">
                {rows.map((row) => {
                  const checked = selected.has(row.id);
                  return (
                    <li key={row.id}>
                      <label className="flex cursor-pointer items-center gap-3 px-4 py-2.5 hover:bg-muted/40">
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleRow(row.id)}
                          className="size-4 accent-[var(--accent)]"
                        />
                        <div className="min-w-0 flex-1">
                          {isIntakeRow(row) ? (
                            <>
                              <p className="truncate text-sm font-medium">
                                {row.name || "Unnamed"}
                                {row.flags > 0 && (
                                  <span className="ml-2 inline-flex items-center gap-0.5 text-xs text-[var(--live)]">
                                    <TriangleAlert className="size-3" />
                                    {row.flags}
                                  </span>
                                )}
                              </p>
                              <p className="text-xs text-muted-foreground">
                                {row.phone || "no phone"} ·{" "}
                                {new Date(row.created_at).toLocaleString()}
                              </p>
                            </>
                          ) : (
                            <>
                              <p className="truncate text-sm font-medium">
                                {(row as ConsultationRow).title}
                              </p>
                              <p className="text-xs text-muted-foreground">
                                {new Date(row.created_at).toLocaleString()}
                                {(row as ConsultationRow).duration_s
                                  ? ` · ${Math.round((row as ConsultationRow).duration_s! / 60)} min`
                                  : ""}
                              </p>
                            </>
                          )}
                        </div>
                        <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-[10px] font-medium capitalize text-muted-foreground">
                          {row.status.replace("_", " ")}
                        </span>
                      </label>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </div>

        {/* Download bar */}
        <div className="sticky bottom-4 flex flex-col gap-2">
          {downloadError && (
            <p className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-2.5 text-sm text-destructive">
              {downloadError}
            </p>
          )}
          <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-border bg-card/80 p-3 backdrop-blur-sm">
            <p className="pl-2 text-sm text-muted-foreground">
              Exporting{" "}
              <span className="font-semibold text-foreground">
                {selected.size ? `${selected.size} selected` : `all ${count}`}
              </span>{" "}
              record{exportCount === 1 ? "" : "s"}
            </p>
            <div className="ml-auto flex gap-2">
              <button
                onClick={() => download("csv")}
                disabled={downloading !== null || exportCount === 0}
                className="press inline-flex items-center gap-2 rounded-full border border-border bg-card px-4 py-2 text-sm font-medium hover:bg-muted disabled:opacity-50"
              >
                {downloading === "csv" ? (
                  <Loader2 className="size-4 animate-spin" />
                ) : (
                  <FileText className="size-4" />
                )}
                CSV
              </button>
              <button
                onClick={() => download("xlsx")}
                disabled={downloading !== null || exportCount === 0}
                className="press inline-flex items-center gap-2 rounded-full bg-[var(--accent)] px-4 py-2 text-sm font-semibold text-white shadow-sm disabled:opacity-50"
              >
                {downloading === "xlsx" ? (
                  <Loader2 className="size-4 animate-spin" />
                ) : (
                  <FileSpreadsheet className="size-4" />
                )}
                Excel
              </button>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
