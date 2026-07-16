import { Activity, CircleCheck, CircleDashed, HeartPulse, User } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { INTAKE_FIELDS, type FieldDef } from "./fields";
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

function FieldValue({ value }: { value: unknown }) {
  // Nothing to draw for a field the bot hasn't collected yet: the dashed icon
  // and muted label already read as pending, and a placeholder glyph in every
  // empty row just adds a column of noise.
  if (!isFilled(value)) return null;

  // Symptoms, allergies, conditions and medications all arrive as lists. Chips
  // make a five-symptom answer scannable where a comma-joined string doesn't.
  if (Array.isArray(value)) {
    return (
      <span className="flex flex-wrap gap-1">
        {value.map((item, i) => (
          <Badge key={i} variant="secondary" className="font-normal">
            {typeof item === "string" ? item : JSON.stringify(item)}
          </Badge>
        ))}
      </span>
    );
  }

  return <span className="font-medium break-words">{String(value)}</span>;
}

export default function IntakeForm({ values }: { values: FieldValues }) {
  const required = INTAKE_FIELDS.filter((f) => f.required);
  const done = required.filter((f) => isFilled(getNested(values, f.key))).length;
  const complete = done === required.length;

  return (
    <Card className="h-full">
      <CardHeader className="border-b">
        <CardTitle className="text-base">Intake record</CardTitle>
        <CardAction>
          <Badge variant={complete ? "default" : "secondary"}>
            {complete ? "Complete" : `${done} of ${required.length}`}
          </Badge>
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
                  const filled = isFilled(raw);
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
                        <FieldValue value={raw} />
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
