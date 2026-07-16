import { useState } from "react";
import {
  Activity,
  ClipboardCheck,
  Copy,
  FlaskConical,
  HeartPulse,
  ListChecks,
  Pill,
  ShieldAlert,
  Stethoscope,
  TriangleAlert,
  User,
} from "lucide-react";
import type { ClinicalSummary } from "./api";

function hasItems(v: unknown[]): boolean {
  return Array.isArray(v) && v.length > 0;
}

/** A list section — rendered only when it has content, so the note reads tight. */
function ListSection({
  icon: Icon,
  title,
  items,
  tone = "default",
}: {
  icon: React.ElementType;
  title: string;
  items: string[];
  tone?: "default" | "danger";
}) {
  if (!hasItems(items)) return null;
  return (
    <section className="space-y-2">
      <h3 className="flex items-center gap-2 text-sm font-semibold tracking-tight">
        <Icon className={`size-4 ${tone === "danger" ? "text-[var(--live)]" : "text-[var(--accent)]"}`} />
        {title}
      </h3>
      <ul className="space-y-1.5 pl-6">
        {items.map((item, i) => (
          <li
            key={i}
            className={`relative text-sm leading-relaxed before:absolute before:-left-4 before:top-2.5 before:size-1.5 before:rounded-full ${
              tone === "danger" ? "before:bg-[var(--live)]" : "before:bg-[var(--accent)]/50"
            }`}
          >
            {item}
          </li>
        ))}
      </ul>
    </section>
  );
}

function summaryToText(s: ClinicalSummary): string {
  const lines: string[] = [];
  const add = (label: string, val: string) => val?.trim() && lines.push(`${label}: ${val.trim()}`);
  const addList = (label: string, arr: string[]) =>
    hasItems(arr) && lines.push(`${label}:`, ...arr.map((x) => `  - ${x}`));

  add("Chief complaint", s.chief_complaint);
  add("History of present illness", s.history_of_present_illness);
  addList("Symptoms", s.symptoms);
  addList("Vitals & examination", s.vitals_and_exam);
  addList("Past history", s.past_history);
  addList("Current medications", s.current_medications);
  addList("Assessment", s.assessment);
  addList("Investigations ordered", s.investigations_ordered);
  if (hasItems(s.medications_prescribed)) {
    lines.push("Medications prescribed:");
    for (const m of s.medications_prescribed) {
      lines.push(
        `  - ${[m.name, m.dosage, m.frequency, m.duration].filter(Boolean).join(" · ")}`,
      );
    }
  }
  addList("Advice & plan", s.advice_and_plan);
  add("Follow-up", s.follow_up);
  addList("Return immediately if", s.red_flags);
  add("Patient summary", s.patient_summary);
  return lines.join("\n");
}

export default function SummaryNote({ summary }: { summary: ClinicalSummary }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(summaryToText(summary));
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      /* clipboard blocked — no-op, the note is still on screen */
    }
  };

  return (
    <div className="space-y-6">
      {/* Responsibility (§16.3): an AI note in a clinical setting must announce
          itself and defer to the clinician. */}
      <div className="flex items-start gap-2.5 rounded-2xl border border-amber-500/25 bg-amber-500/10 px-4 py-3 text-sm text-amber-700 dark:text-amber-300">
        <ShieldAlert className="mt-0.5 size-4 shrink-0" />
        <p className="leading-relaxed">
          AI-generated from the conversation. It can mishear names, doses, and numbers. Review and
          correct before it enters the record.
        </p>
      </div>

      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Chief complaint
          </p>
          <h2 className="text-xl font-semibold leading-tight tracking-tight">
            {summary.chief_complaint || "Not stated"}
          </h2>
        </div>
        <button
          onClick={copy}
          className="press inline-flex shrink-0 items-center gap-1.5 rounded-full border border-border bg-card px-3 py-1.5 text-xs font-medium text-muted-foreground hover:text-foreground"
        >
          {copied ? <ClipboardCheck className="size-3.5 text-[var(--accent)]" /> : <Copy className="size-3.5" />}
          {copied ? "Copied" : "Copy note"}
        </button>
      </div>

      {summary.history_of_present_illness?.trim() && (
        <section className="space-y-2">
          <h3 className="flex items-center gap-2 text-sm font-semibold tracking-tight">
            <Stethoscope className="size-4 text-[var(--accent)]" />
            History of present illness
          </h3>
          <p className="pl-6 text-sm leading-relaxed text-foreground/90">
            {summary.history_of_present_illness}
          </p>
        </section>
      )}

      <ListSection icon={Activity} title="Symptoms" items={summary.symptoms} />
      <ListSection icon={HeartPulse} title="Vitals & examination" items={summary.vitals_and_exam} />
      <ListSection icon={User} title="Past history" items={summary.past_history} />
      <ListSection icon={Pill} title="Current medications" items={summary.current_medications} />
      <ListSection icon={ClipboardCheck} title="Assessment" items={summary.assessment} />
      <ListSection icon={FlaskConical} title="Investigations ordered" items={summary.investigations_ordered} />

      {hasItems(summary.medications_prescribed) && (
        <section className="space-y-2">
          <h3 className="flex items-center gap-2 text-sm font-semibold tracking-tight">
            <Pill className="size-4 text-[var(--accent)]" />
            Medications prescribed
          </h3>
          <div className="space-y-2 pl-6">
            {summary.medications_prescribed.map((m, i) => (
              <div key={i} className="rounded-xl border border-border bg-card/60 px-3.5 py-2.5">
                <p className="text-sm font-medium">{m.name}</p>
                {[m.dosage, m.frequency, m.duration].some(Boolean) && (
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {[m.dosage, m.frequency, m.duration].filter(Boolean).join("  ·  ")}
                  </p>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      <ListSection icon={ListChecks} title="Advice & plan" items={summary.advice_and_plan} />

      {summary.follow_up?.trim() && (
        <section className="space-y-2">
          <h3 className="flex items-center gap-2 text-sm font-semibold tracking-tight">
            <ClipboardCheck className="size-4 text-[var(--accent)]" />
            Follow-up
          </h3>
          <p className="pl-6 text-sm leading-relaxed">{summary.follow_up}</p>
        </section>
      )}

      {hasItems(summary.red_flags) && (
        <section className="space-y-2 rounded-2xl border border-[var(--live)]/25 bg-[var(--live)]/8 p-4">
          <h3 className="flex items-center gap-2 text-sm font-semibold tracking-tight text-[var(--live)]">
            <TriangleAlert className="size-4" />
            Return immediately if
          </h3>
          <ul className="space-y-1.5 pl-6">
            {summary.red_flags.map((item, i) => (
              <li
                key={i}
                className="relative text-sm leading-relaxed before:absolute before:-left-4 before:top-2.5 before:size-1.5 before:rounded-full before:bg-[var(--live)]"
              >
                {item}
              </li>
            ))}
          </ul>
        </section>
      )}

      {summary.patient_summary?.trim() && (
        <section className="space-y-2 rounded-2xl bg-[var(--accent-soft)] p-4">
          <h3 className="flex items-center gap-2 text-sm font-semibold tracking-tight">
            <User className="size-4 text-[var(--accent)]" />
            In plain language
          </h3>
          <p className="text-sm leading-relaxed text-foreground/90">{summary.patient_summary}</p>
        </section>
      )}
    </div>
  );
}
