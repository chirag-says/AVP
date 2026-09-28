// The intake page's state machine. Pure (no React, no I/O) so it can be
// tested directly: `node --test src/intake/*.test.ts`.
//
//   IDLE -> CONNECTING -> IN_PROGRESS -> FINALIZING -> SAVING -> COMPLETED
//                                                         \-> SAVE_FAILED -> SAVING (retry)
//   COMPLETED -> STARTING_NEXT_PATIENT -> CONNECTING -> ...   (fresh patient state)
//   any -> ERROR (connection failure)
//
// Everything patient-specific lives in this one object, and starting the next
// patient replaces it wholesale. Each connection gets a `generation`; actions
// from an older connection (a late transcript or field update from Patient A
// arriving after Patient B started) carry the old generation and are ignored.

export type Phase =
  | "IDLE"
  | "CONNECTING"
  | "IN_PROGRESS"
  | "FINALIZING"
  | "SAVING"
  | "COMPLETED"
  | "SAVE_FAILED"
  | "STARTING_NEXT_PATIENT"
  | "ERROR";

export interface TurnEntry {
  role: "patient" | "bot";
  text: string;
}

export interface IntakeState {
  phase: Phase;
  generation: number;
  turns: TurnEntry[];
  fields: Record<string, unknown>;
  fieldStatus: Record<string, string>;
  sessionId: string | null;
  patientName: string | null;
  message: string | null; // error or save-problem text
}

export type IntakeAction =
  | { type: "CONNECT_START"; newGen: number }
  | { type: "CONNECTED"; gen: number }
  | { type: "DISCONNECTED"; gen: number }
  | { type: "CONNECT_FAILED"; gen: number; message: string }
  | { type: "TURN"; gen: number; role: TurnEntry["role"]; text: string }
  | { type: "FIELD_UPDATE"; gen: number; key: string; value: unknown; status?: string }
  | { type: "FINALIZING"; gen: number }
  | { type: "SAVING"; gen: number }
  | { type: "COMPLETE"; gen: number; sessionId: string | null; patientName: string | null }
  | { type: "SAVE_FAILED"; gen: number; message: string }
  | { type: "START_NEXT_PATIENT"; newGen: number };

export function initialIntakeState(generation = 0): IntakeState {
  return {
    phase: "IDLE",
    generation,
    turns: [],
    fields: {},
    fieldStatus: {},
    sessionId: null,
    patientName: null,
    message: null,
  };
}

// Phases in which the patient is done: nothing more may be recorded.
const LOCKED: Phase[] = ["FINALIZING", "SAVING", "COMPLETED", "SAVE_FAILED", "STARTING_NEXT_PATIENT"];

function setNested(fields: Record<string, unknown>, dotted: string, value: unknown) {
  const next = structuredClone(fields);
  const parts = dotted.split(".");
  let node = next as Record<string, unknown>;
  for (const part of parts.slice(0, -1)) {
    if (typeof node[part] !== "object" || node[part] === null) node[part] = {};
    node = node[part] as Record<string, unknown>;
  }
  node[parts[parts.length - 1]] = value;
  return next;
}

export function intakeReducer(state: IntakeState, action: IntakeAction): IntakeState {
  // A message from a previous patient's connection never touches this one.
  if ("gen" in action && action.gen !== state.generation) return state;

  switch (action.type) {
    case "CONNECT_START":
      // A new connection means a new patient: start from nothing.
      return { ...initialIntakeState(action.newGen), phase: "CONNECTING" };
    case "CONNECTED":
      return state.phase === "CONNECTING" ? { ...state, phase: "IN_PROGRESS" } : state;
    case "CONNECT_FAILED":
      return { ...state, phase: "ERROR", message: action.message };
    case "DISCONNECTED":
      if (state.phase === "COMPLETED" || state.phase === "STARTING_NEXT_PATIENT") return state;
      if (state.phase === "SAVING" || state.phase === "FINALIZING") {
        return {
          ...state,
          phase: "ERROR",
          message: "The connection closed while saving. Check Reception records before entering this patient again.",
        };
      }
      return { ...state, phase: state.phase === "ERROR" ? "ERROR" : "IDLE" };
    case "TURN":
      if (state.phase === "IDLE" || state.phase === "STARTING_NEXT_PATIENT") return state;
      return { ...state, turns: [...state.turns, { role: action.role, text: action.text }] };
    case "FIELD_UPDATE":
      if (LOCKED.includes(state.phase) || state.phase === "IDLE") return state;
      return {
        ...state,
        fields: setNested(state.fields, action.key, action.value),
        fieldStatus: action.status ? { ...state.fieldStatus, [action.key]: action.status } : state.fieldStatus,
      };
    case "FINALIZING":
      return state.phase === "IN_PROGRESS" ? { ...state, phase: "FINALIZING" } : state;
    case "SAVING":
      return ["IN_PROGRESS", "FINALIZING", "SAVE_FAILED"].includes(state.phase)
        ? { ...state, phase: "SAVING", message: null }
        : state;
    case "COMPLETE":
      // Only the server's confirmation of a successful save gets here.
      return { ...state, phase: "COMPLETED", sessionId: action.sessionId, patientName: action.patientName };
    case "SAVE_FAILED":
      return { ...state, phase: "SAVE_FAILED", message: action.message };
    case "START_NEXT_PATIENT":
      return { ...initialIntakeState(action.newGen), phase: "STARTING_NEXT_PATIENT" };
  }
}

export const isLocked = (phase: Phase) => LOCKED.includes(phase);
