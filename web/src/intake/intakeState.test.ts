// node --test src/intake/*.test.ts   (Node 24 runs TypeScript directly)
import { test } from "node:test";
import assert from "node:assert/strict";
import { initialIntakeState, intakeReducer, type IntakeAction, type IntakeState } from "./intakeState.ts";

const run = (actions: IntakeAction[], from: IntakeState = initialIntakeState()) => actions.reduce(intakeReducer, from);

const patientA = (gen: number): IntakeAction[] => [
  { type: "CONNECT_START", newGen: gen },
  { type: "CONNECTED", gen },
  { type: "TURN", gen, role: "bot", text: "Welcome to AVP Hospital..." },
  { type: "TURN", gen, role: "patient", text: "My name is Chirag" },
  { type: "FIELD_UPDATE", gen, key: "personal.full_name", value: "Chirag", status: "answered" },
  { type: "FIELD_UPDATE", gen, key: "personal.age", value: "22", status: "answered" },
  { type: "FIELD_UPDATE", gen, key: "personal.phone", value: "9876543210", status: "confirmed" },
  { type: "FIELD_UPDATE", gen, key: "medical_history.allergies", value: "none", status: "answered" },
];

test("T1: normal intake -> completion only after the server's save confirmation", () => {
  const saving = run([...patientA(1), { type: "FINALIZING", gen: 1 }, { type: "SAVING", gen: 1 }]);
  assert.equal(saving.phase, "SAVING"); // modal (open only when COMPLETED) not shown yet
  const done = intakeReducer(saving, { type: "COMPLETE", gen: 1, sessionId: "row-1", patientName: "Chirag" });
  assert.equal(done.phase, "COMPLETED");
  assert.equal(done.patientName, "Chirag");
  assert.equal(done.sessionId, "row-1");
});

test("T5: save failure -> SAVE_FAILED, never COMPLETED; retry goes back to SAVING", () => {
  const failed = run([...patientA(1), { type: "SAVING", gen: 1 }, { type: "SAVE_FAILED", gen: 1, message: "problem" }]);
  assert.equal(failed.phase, "SAVE_FAILED");
  assert.notEqual(failed.phase, "COMPLETED");
  assert.equal(intakeReducer(failed, { type: "SAVING", gen: 1 }).phase, "SAVING");
});

test("after completion nothing more can be recorded (patient can't keep talking into the record)", () => {
  const done = run([...patientA(1), { type: "SAVING", gen: 1 }, { type: "COMPLETE", gen: 1, sessionId: "r", patientName: "Chirag" }]);
  const after = intakeReducer(done, { type: "FIELD_UPDATE", gen: 1, key: "personal.age", value: "99" });
  assert.equal((after.fields.personal as Record<string, unknown>).age, "22");
  assert.equal(intakeReducer(done, { type: "DISCONNECTED", gen: 1 }).phase, "COMPLETED"); // server closes the call
});

test("T7: Start Next Patient clears every patient-specific value", () => {
  const done = run([...patientA(1), { type: "SAVING", gen: 1 }, { type: "COMPLETE", gen: 1, sessionId: "r", patientName: "Chirag" }]);
  const next = intakeReducer(done, { type: "START_NEXT_PATIENT", newGen: 2 });
  assert.equal(next.phase, "STARTING_NEXT_PATIENT");
  assert.deepEqual(next.turns, []);
  assert.deepEqual(next.fields, {});
  assert.deepEqual(next.fieldStatus, {});
  assert.equal(next.sessionId, null);
  assert.equal(next.patientName, null);
  assert.equal(next.message, null);
});

test("T10: patient A completes, patient B starts with name/age/phone/allergies all empty, and late A messages are ignored", () => {
  const afterA = run([...patientA(1), { type: "SAVING", gen: 1 }, { type: "COMPLETE", gen: 1, sessionId: "r1", patientName: "Chirag" }]);
  let b = run([{ type: "START_NEXT_PATIENT", newGen: 2 }, { type: "CONNECT_START", newGen: 3 }, { type: "CONNECTED", gen: 3 }], afterA);
  // A's connection is still closing: its late transcript/field/completion events arrive now.
  b = run(
    [
      { type: "TURN", gen: 1, role: "bot", text: "Thank you, Chirag. Your information has been saved." },
      { type: "FIELD_UPDATE", gen: 1, key: "personal.full_name", value: "Chirag" },
      { type: "COMPLETE", gen: 1, sessionId: "r1", patientName: "Chirag" },
      { type: "DISCONNECTED", gen: 1 },
    ],
    b,
  );
  const get = (k: string) => k.split(".").reduce<any>((n, p) => (n == null ? undefined : n[p]), b.fields);
  for (const key of ["personal.full_name", "personal.age", "personal.phone", "medical_history.allergies"]) {
    assert.equal(get(key), undefined, key);
  }
  assert.equal(b.phase, "IN_PROGRESS");
  assert.deepEqual(b.turns, []);
  assert.equal(b.patientName, null);
  assert.equal(JSON.stringify(b).includes("Chirag"), false);
  assert.equal(JSON.stringify(b).includes("9876543210"), false);
});

test("disconnect while saving surfaces an error instead of pretending success", () => {
  const s = run([...patientA(1), { type: "SAVING", gen: 1 }, { type: "DISCONNECTED", gen: 1 }]);
  assert.equal(s.phase, "ERROR");
  assert.match(s.message ?? "", /closed while saving/);
});
