import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  ClipboardList,
  Database,
  Mic,
  RefreshCw,
  ScrollText,
  Sparkles,
  Square,
  Stethoscope,
  TriangleAlert,
} from "lucide-react";
import ListeningOrb from "./ListeningOrb";
import SummaryNote from "./SummaryNote";
import HistoryView from "./HistoryView";
import { useScribeSession } from "./useScribeSession";
import { summarizeConsultation, type Segment, type SummarizeResponse } from "./api";

function fmtClock(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

type Mode = "record" | "history";

export default function ConsultationScribe() {
  const session = useScribeSession();
  const [mode, setMode] = useState<Mode>("record");

  const [summary, setSummary] = useState<SummarizeResponse | null>(null);
  const [summarizing, setSummarizing] = useState(false);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  // Snapshot of the transcript being summarised, held so a failed summary can be
  // retried without re-recording — the whole point of the "keep transcript" fix.
  const [captured, setCaptured] = useState<Segment[]>([]);
  const [capturedDuration, setCapturedDuration] = useState(0);

  const [elapsed, setElapsed] = useState(0);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const listening = session.status === "listening";

  // Elapsed timer runs only while actually listening.
  useEffect(() => {
    if (!listening) return;
    setElapsed(0);
    const started = Date.now();
    const id = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 250);
    return () => clearInterval(id);
  }, [listening]);

  // Keep the live transcript pinned to the newest line.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [session.segments]);

  const runSummary = useCallback(async (transcript: Segment[], durationS: number) => {
    setSummarizing(true);
    setSummaryError(null);
    try {
      const res = await summarizeConsultation(transcript, durationS);
      setSummary(res);
    } catch (err) {
      setSummaryError(err instanceof Error ? err.message : "Could not generate the summary.");
    } finally {
      setSummarizing(false);
    }
  }, []);

  const endAndSummarize = useCallback(async () => {
    const snapshot = session.segments;
    const durationS = elapsed;
    await session.stop();
    setCaptured(snapshot);
    setCapturedDuration(durationS);

    if (snapshot.length === 0) {
      // Nothing was heard — don't ask the model to summarise silence.
      setSummaryError("Nothing was captured. Check the microphone and try again.");
      return;
    }
    await runSummary(snapshot, durationS);
  }, [session, elapsed, runSummary]);

  const newConsultation = useCallback(() => {
    setSummary(null);
    setSummaryError(null);
    setSummarizing(false);
    setCaptured([]);
    session.reset();
  }, [session]);

  // --- screens ----------------------------------------------------------

  const Transcript = (
    <div
      ref={scrollRef}
      className="max-h-[42vh] space-y-2.5 overflow-y-auto rounded-2xl border border-border bg-card/50 p-4"
    >
      {session.segments.length === 0 ? (
        <p className="py-8 text-center text-sm text-muted-foreground">
          The conversation will appear here as you both speak.
        </p>
      ) : (
        session.segments.map((seg, i) => (
          <p key={i} className="seg-enter text-sm leading-relaxed">
            <span className="text-foreground/90">{seg.text}</span>
          </p>
        ))
      )}
    </div>
  );

  let screen: React.ReactNode;

  if (summary) {
    screen = (
      <div className="view-enter mx-auto max-w-2xl">
        <SummaryNote summary={summary.summary} />
        <div className="mt-8 flex items-center justify-center gap-3">
          <button
            onClick={newConsultation}
            className="press inline-flex items-center gap-2 rounded-full bg-[var(--accent)] px-5 py-2.5 text-sm font-medium text-white shadow-sm"
          >
            <Mic className="size-4" /> New consultation
          </button>
        </div>
        {!summary.id && (
          <p className="mt-4 text-center text-xs text-muted-foreground">
            Not archived — the database isn't configured, so this note won't be saved.
          </p>
        )}
      </div>
    );
  } else if (summarizing) {
    screen = (
      <div className="view-enter mx-auto flex max-w-2xl flex-col items-center gap-6 py-6">
        <div className="flex flex-col items-center gap-3 text-center">
          <div className="relative grid size-16 place-items-center rounded-full bg-[var(--accent-soft)]">
            <Sparkles className="size-6 animate-pulse text-[var(--accent)]" />
          </div>
          <div>
            <p className="font-medium">Writing the clinical note</p>
            <p className="text-sm text-muted-foreground">
              Reading {captured.length} exchanges from the conversation…
            </p>
          </div>
        </div>
        <div className="w-full">{Transcript}</div>
      </div>
    );
  } else if (summaryError) {
    // The summary failed (usually the model was briefly overloaded). Keep the
    // transcript on screen and let them retry it — never silently drop it.
    const canRetry = captured.length > 0;
    screen = (
      <div className="view-enter mx-auto flex max-w-2xl flex-col gap-6">
        <div className="flex flex-col items-center gap-3 text-center">
          <div className="grid size-14 place-items-center rounded-full bg-destructive/10">
            <TriangleAlert className="size-6 text-destructive" />
          </div>
          <div>
            <p className="font-medium">The summary didn&apos;t go through</p>
            <p className="mx-auto mt-1 max-w-md text-sm text-muted-foreground">{summaryError}</p>
          </div>
        </div>

        {canRetry ? (
          <>
            <div className="rounded-2xl border border-border bg-card/50 p-4">
              <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                Your transcript is safe · {captured.length} lines
              </p>
              <div className="max-h-[32vh] space-y-2 overflow-y-auto">
                {captured.map((seg, i) => (
                  <p key={i} className="text-sm leading-relaxed text-foreground/90">
                    {seg.text}
                  </p>
                ))}
              </div>
            </div>
            <div className="flex items-center justify-center gap-3">
              <button
                onClick={() => runSummary(captured, capturedDuration)}
                className="press inline-flex items-center gap-2 rounded-full bg-[var(--accent)] px-5 py-2.5 text-sm font-semibold text-white shadow-sm"
              >
                <RefreshCw className="size-4" /> Retry summary
              </button>
              <button
                onClick={newConsultation}
                className="press inline-flex items-center gap-2 rounded-full border border-border bg-card px-5 py-2.5 text-sm font-medium text-muted-foreground hover:text-foreground"
              >
                Discard
              </button>
            </div>
          </>
        ) : (
          <div className="flex justify-center">
            <button
              onClick={newConsultation}
              className="press inline-flex items-center gap-2 rounded-full bg-[var(--accent)] px-5 py-2.5 text-sm font-semibold text-white shadow-sm"
            >
              <Mic className="size-4" /> Start over
            </button>
          </div>
        )}
      </div>
    );
  } else if (listening || session.status === "connecting") {
    const connecting = session.status === "connecting";
    screen = (
      <div className="view-enter mx-auto flex max-w-2xl flex-col items-center gap-7">
        <ListeningOrb listening={listening} levelRef={session.levelRef} />
        <div className="text-center">
          <div className="font-mono text-3xl font-semibold tabular-nums tracking-tight">
            {fmtClock(elapsed)}
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {connecting ? "Connecting…" : "Listening to the consultation"}
          </p>
        </div>

        <div className="w-full">{Transcript}</div>

        <button
          onClick={endAndSummarize}
          disabled={connecting}
          className="press inline-flex items-center gap-2 rounded-full bg-[var(--live)] px-6 py-3 text-sm font-semibold text-white shadow-sm disabled:opacity-50"
        >
          <Square className="size-4 fill-current" /> End &amp; summarize
        </button>
      </div>
    );
  } else {
    // idle — summary failures are handled by their own screen above, so only a
    // connection error can surface here.
    const err = session.errorMsg;
    screen = (
      <div className="view-enter mx-auto flex max-w-xl flex-col items-center gap-8 pt-4 text-center">
        <ListeningOrb listening={false} levelRef={session.levelRef} />
        <div className="space-y-2">
          <h1 className="text-3xl font-semibold tracking-tight">Ready to listen</h1>
          <p className="text-pretty leading-relaxed text-muted-foreground">
            Start the session at the beginning of the visit. The scribe listens quietly to you and
            the patient, shows a live transcript, and writes a structured clinical note when you
            end.
          </p>
        </div>

        {err && (
          <p className="w-full rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-2.5 text-sm text-destructive">
            {err}
          </p>
        )}

        <button
          onClick={session.start}
          className="press inline-flex items-center gap-2.5 rounded-full bg-[var(--accent)] px-7 py-3.5 text-base font-semibold text-white shadow-md"
        >
          <Mic className="size-5" /> Start listening
        </button>

        {/* Responsibility (§16.3): consent is a real step, so name it. */}
        <p className="max-w-md text-xs leading-relaxed text-muted-foreground">
          Please let the patient know the conversation is being transcribed. Audio is used only to
          create this note.
        </p>
      </div>
    );
  }

  const showBackToRecord = mode === "history";

  return (
    <div className="scribe bg-background text-foreground" data-listening={listening}>
      <div className="scribe-ambient" aria-hidden />

      <header className="scribe-header">
        <div className="mx-auto flex h-14 max-w-3xl items-center gap-3 px-5">
          <ScrollText className="size-5 shrink-0 text-[var(--accent)]" />
          <div className="leading-tight">
            <span className="font-semibold tracking-tight">Consultation Scribe</span>
          </div>

          <div className="ml-auto flex items-center gap-3">
            {listening && (
              <span className="flex items-center gap-2 text-xs font-medium text-[var(--live)]">
                <span className="live-dot" /> {fmtClock(elapsed)}
              </span>
            )}

            {/* Nav is hidden mid-session so nothing interrupts a live recording
                (Agency + safety — don't offer a path that drops audio). */}
            {!listening && session.status !== "connecting" && !summarizing && (
              <>
                <a
                  href="/"
                  className="press inline-flex items-center gap-1.5 rounded-full border border-border bg-card/60 px-3 py-1.5 text-xs font-medium text-muted-foreground hover:text-foreground"
                >
                  <Stethoscope className="size-3.5" /> Reception
                </a>
                <a
                  href="/export.html"
                  className="press inline-flex items-center gap-1.5 rounded-full border border-border bg-card/60 px-3 py-1.5 text-xs font-medium text-muted-foreground hover:text-foreground"
                >
                  <Database className="size-3.5" /> Export
                </a>
                <button
                  onClick={() => setMode(showBackToRecord ? "record" : "history")}
                  className="press inline-flex items-center gap-1.5 rounded-full border border-border bg-card/60 px-3 py-1.5 text-xs font-medium text-muted-foreground hover:text-foreground"
                >
                  {showBackToRecord ? (
                    <>
                      <ArrowLeft className="size-3.5" /> Back
                    </>
                  ) : (
                    <>
                      <ClipboardList className="size-3.5" /> Past notes
                    </>
                  )}
                </button>
              </>
            )}
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-3xl px-5 py-10 sm:py-14">
        {mode === "history" ? <HistoryView /> : screen}
      </main>
    </div>
  );
}
