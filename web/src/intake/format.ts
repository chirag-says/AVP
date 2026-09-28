// Patient-facing rendering of intake values. Pure, so it can be tested with
// `node --test`. Raw JSON never reaches the screen.

export interface SymptomView {
  description: string;
  duration: string | null;
  severity: string | null;
}

function capitalize(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

function clean(v: unknown): string | null {
  if (v == null) return null;
  const s = String(v).trim();
  return s && !["null", "none", "unknown", "n/a"].includes(s.toLowerCase()) ? s : null;
}

/** "5-6 years" -> "5–6 years" (a range reads better with an en dash). */
function range(text: string): string {
  return text.replace(/(\d)\s*-\s*(\d)/g, "$1–$2");
}

export function toSymptoms(value: unknown): SymptomView[] {
  const items = Array.isArray(value) ? value : value == null ? [] : [value];
  return items.flatMap((item) => {
    if (typeof item === "string") return item.trim() ? [{ description: capitalize(item.trim()), duration: null, severity: null }] : [];
    if (item && typeof item === "object") {
      const o = item as Record<string, unknown>;
      const description = clean(o.description ?? o.name ?? o.symptom);
      if (!description) return [];
      const duration = clean(o.duration);
      return [{ description: capitalize(description), duration: duration && range(duration), severity: clean(o.severity) }];
    }
    return [];
  });
}

/** Display text for a non-symptom value, or null when there is nothing to show. */
export function displayValue(value: unknown): string | string[] | null {
  if (value == null) return null;
  if (typeof value === "string") {
    const s = value.trim();
    if (!s) return null;
    if (s.toLowerCase() === "none") return "None";
    if (s.toLowerCase() === "declined") return "Declined";
    return s;
  }
  if (Array.isArray(value)) {
    const items = value
      .map((v) => (typeof v === "string" ? v.trim() : v && typeof v === "object" ? toSymptoms([v])[0]?.description : String(v)))
      .filter((v): v is string => Boolean(v));
    if (items.length === 1 && items[0].toLowerCase() === "none") return "None";
    return items.length ? items.map(capitalize) : null;
  }
  if (typeof value === "object") return null; // never render an object as JSON
  return String(value);
}

export const STATUS_LABEL: Record<string, string> = {
  needs_confirmation: "Confirming",
  skipped: "Skipped",
};
