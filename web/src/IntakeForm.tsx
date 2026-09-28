import { Activity, CircleCheck, CircleDashed, HeartPulse, User } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { INTAKE_FIELDS, type FieldDef } from "./fields";
import { STATUS_LABEL, displayValue, toSymptoms } from "./intake/format";
import { Badge } from "@/components/ui/badge";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";

type FieldValues = Record<string, unknown>;

// Presentation only — the field list itself still comes from fields.ts, which
// mirrors the server schema. Adding a field there slots it into the right
// section here automatically; a new *section* is the only thing that needs a
// line added below.
const SECTIONS: Record<string, { label: string; icon: LucideIcon }> = {
  personal: { label: "Personal details", icon: User },
  visit: { label: "Visit", icon: Activity },
  medical_history: { label: "Medical history", icon: HeartPulse },
};

function getNested(data: FieldValues, dotted: string): unknown {
  return dotted.split(".").reduce<any>((node, part) => (node == null ? undefined : node[part]), data);
}

function isFilled(v: unknown): boolean {
  if (v == null || v === "") return false;
  return Array.isArray(v) ? v.length > 0 : String(v).trim() !== "";
}

// A field counts as done once the server recorded it (answered, confirmed or
// skipped); a value awaiting the patient's confirmation does not.
function isDone(value: unknown, status: string | undefined): boolean {
  if (status === "needs_confirmation") return false;
  return status === "skipped" || isFilled(value);
}

/** Groups fields by their key prefix, preserving the order fields.ts declares. */
function groupFields(fields: FieldDef[]) {
  const groups: { key: string; fields: FieldDef[] }[] = [];
  for (const field of fields) {
    const key = field.key.split(".")[0];
    const last = groups.at(-1);
    if (last?.key === key) last.fields.push(field);
    else groups.push({ key, fields: [field] });
  }
  return groups;
}

function FieldValue({ fieldKey, value }: { fieldKey: string; value: unknown }) {
  // Nothing to draw for a field the bot hasn't collected yet: the dashed icon
  // and muted label already read as pending.
  if (!isFilled(value)) return null;

  if (fieldKey === "visit.symptoms") {
    const symptoms = toSymptoms(value);
    if (!symptoms.length) return null;
    return (
      <ul className="flex flex-col gap-1.5">
        {symptoms.map((s, i) => (
          <li key={i}>
            <span className="font-medium">{s.description}</span>
            {(s.duration || s.severity) && (
              <span className="block text-xs text-muted-foreground">
                {[s.duration && `Duration: ${s.duration}`, s.severity && `Severity: ${s.severity}`]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
            )}
          </li>
        ))}
      </ul>
    );
  }

  const shown = displayValue(value);
  if (shown == null) return null;
  // Conditions and medications arrive as lists. Chips make a five-item
  // answer scannable where a comma-joined string doesn't.
  if (Array.isArray(shown)) {
    return (
      <span className="flex flex-wrap gap-1">
        {shown.map((item, i) => (
          <Badge key={i} variant="secondary" className="font-normal">
            {item}
          </Badge>
        ))}
      </span>
    );
  }
  return <span className="font-medium break-words">{shown}</span>;
}

export default function IntakeForm({
  values,
  status = {},
  saved = false,
}: {
  values: FieldValues;
  status?: Record<string, string>;
  saved?: boolean;
}) {
  const required = INTAKE_FIELDS.filter((f) => f.required);
  const done = required.filter((f) => isDone(getNested(values, f.key), status[f.key])).length;

  return (
    <Card className="h-full">
      <CardHeader className="border-b">
        <CardTitle className="text-base">Intake record</CardTitle>
        <CardAction>
          {/* "Saved" only after the server confirmed the database write. */}
          <Badge variant={saved ? "default" : "secondary"}>{saved ? "Saved" : `${done} of ${required.length}`}</Badge>
        </CardAction>
        <Progress
          value={(done / required.length) * 100}
          className="mt-2 h-1.5"
          aria-label={`${done} of ${required.length} required fields collected`}
        />
      </CardHeader>

      <CardContent className="flex flex-col gap-5">
        {groupFields(INTAKE_FIELDS).map((group) => {
          const section = SECTIONS[group.key];
          const Icon = section?.icon ?? CircleDashed;
          return (
            <section key={group.key}>
              <h3 className="mb-1 flex items-center gap-1.5 text-xs font-medium tracking-wide text-muted-foreground uppercase">
                <Icon className="size-3.5" />
                {section?.label ?? group.key}
              </h3>
              <ul>
                {group.fields.map((field) => {
                  const raw = getNested(values, field.key);
                  const fieldStatus = status[field.key];
                  const filled = isDone(raw, fieldStatus);
                  return (
                    <li
                      key={field.key}
                      className="flex items-start gap-3 border-b border-border/60 py-2.5 last:border-0"
                    >
                      {filled ? (
                        <CircleCheck className="mt-0.5 size-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
                      ) : (
                        <CircleDashed className="mt-0.5 size-4 shrink-0 text-muted-foreground/40" />
                      )}
                      <span className="w-32 shrink-0 text-muted-foreground">
                        {field.label}
                        {!field.required && (
                          <span className="ml-1 text-xs opacity-60">optional</span>
                        )}
                      </span>
                      <span className="min-w-0 flex-1">
                        {fieldStatus === "needs_confirmation" ? (
                          <span className="text-xs text-amber-700 dark:text-amber-400">
                            {STATUS_LABEL.needs_confirmation}…
                          </span>
                        ) : fieldStatus === "skipped" && !isFilled(raw) ? (
                          <span className="text-xs text-muted-foreground">{STATUS_LABEL.skipped}</span>
                        ) : (
                          <FieldValue fieldKey={field.key} value={raw} />
                        )}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </section>
          );
        })}
      </CardContent>
    </Card>
  );
}
