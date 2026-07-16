// The scribe backend runs as its own process, on its own port, separate from
// the reception chatbot's runner. Point at 7861 by default.
const API_BASE = import.meta.env.VITE_SCRIBE_API_BASE ?? "http://localhost:7861";

export interface Segment {
  text: string;
  ts?: number; // seconds from session start
}

export interface PrescribedMed {
  name: string;
  dosage?: string;
  frequency?: string;
  duration?: string;
}

// Mirrors the JSON contract in server/scribe/summarizer.py (_SCHEMA).
export interface ClinicalSummary {
  chief_complaint: string;
  history_of_present_illness: string;
  symptoms: string[];
  vitals_and_exam: string[];
  past_history: string[];
  current_medications: string[];
  assessment: string[];
  investigations_ordered: string[];
  medications_prescribed: PrescribedMed[];
  advice_and_plan: string[];
  follow_up: string;
  red_flags: string[];
  patient_summary: string;
}

export interface SummarizeResponse {
  id: string | null;
  summary: ClinicalSummary;
  model: string;
}

export interface ConsultationNote {
  id: string;
  created_at: string;
  status: string;
  duration_s: number | null;
  transcript: Segment[];
  summary: ClinicalSummary;
  model: string;
}

export async function summarizeConsultation(
  transcript: Segment[],
  durationS: number,
): Promise<SummarizeResponse> {
  const res = await fetch(`${API_BASE}/api/consultation/summarize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ transcript, duration_s: durationS }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `Summary failed (${res.status})`);
  }
  return res.json();
}

export async function listConsultations(): Promise<ConsultationNote[]> {
  const res = await fetch(`${API_BASE}/api/consultations`);
  if (!res.ok) throw new Error(`Failed to load notes (${res.status})`);
  return res.json();
}
