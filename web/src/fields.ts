// Mirrors server/intake/schema.py — kept in sync manually for the PoC.
// (A production build would serve this from a /schema endpoint instead.)
export interface FieldDef {
  key: string;
  label: string;
  required: boolean;
}

export const INTAKE_FIELDS: FieldDef[] = [
  { key: "personal.full_name", label: "Full name", required: true },
  { key: "personal.age", label: "Age", required: true },
  { key: "personal.gender", label: "Gender", required: true },
  { key: "personal.phone", label: "Mobile number", required: true },
  { key: "personal.address", label: "Address", required: true },
  { key: "visit.symptoms", label: "Symptoms", required: true },
  { key: "medical_history.allergies", label: "Allergies", required: true },
  { key: "medical_history.existing_conditions", label: "Existing conditions", required: false },
  { key: "medical_history.current_medications", label: "Current medications", required: false },
];
