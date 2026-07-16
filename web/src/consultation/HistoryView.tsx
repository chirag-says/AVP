import { useEffect, useState } from "react";
import { ChevronDown, ClipboardList, Loader2 } from "lucide-react";
import SummaryNote from "./SummaryNote";
import { listConsultations, type ConsultationNote } from "./api";

function fmtDuration(seconds: number | null): string {
  if (!seconds) return "";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m ? `${m}m ${s}s` : `${s}s`;
}

export default function HistoryView() {
  const [notes, setNotes] = useState<ConsultationNote[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);

  useEffect(() => {
    listConsultations()
      .then(setNotes)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  if (error) {
    return (
      <p className="view-enter rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
        {error}
      </p>
    );
  }

  if (notes === null) {
    return (
      <div className="view-enter flex items-center justify-center gap-2 py-16 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" /> Loading past notes…
      </div>
    );
  }

  if (notes.length === 0) {
    return (
      <div className="view-enter flex flex-col items-center gap-3 py-16 text-center text-muted-foreground">
        <ClipboardList className="size-8 opacity-40" />
        <p className="text-sm">No consultation notes yet.</p>
      </div>
    );
  }

  return (
    <div className="view-enter mx-auto max-w-2xl space-y-3">
      <h2 className="mb-4 text-lg font-semibold tracking-tight">Past notes</h2>
      {notes.map((note) => {
        const open = openId === note.id;
        return (
          <div key={note.id} className="overflow-hidden rounded-2xl border border-border bg-card/60">
            <button
              onClick={() => setOpenId(open ? null : note.id)}
              className="press flex w-full items-center gap-3 px-4 py-3.5 text-left"
            >
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium">
                  {note.summary?.chief_complaint || "Consultation"}
                </p>
                <p className="text-xs text-muted-foreground">
                  {new Date(note.created_at).toLocaleString()}
                  {note.duration_s ? ` · ${fmtDuration(note.duration_s)}` : ""}
                </p>
              </div>
              <ChevronDown
                className={`size-4 shrink-0 text-muted-foreground transition-transform duration-300 ${
                  open ? "rotate-180" : ""
                }`}
              />
            </button>
            {open && (
              <div className="view-enter border-t border-border px-4 py-5">
                <SummaryNote summary={note.summary} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
