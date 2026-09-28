// node --test src/intake/*.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { displayValue, toSymptoms } from "./format.ts";

test("T9: structured symptoms render as text, never raw JSON", () => {
  const s = toSymptoms([
    { description: "cold", duration: "5-6 years", severity: null },
    { description: "not feeling well", duration: null, severity: "mild" },
  ]);
  assert.deepEqual(s, [
    { description: "Cold", duration: "5–6 years", severity: null },
    { description: "Not feeling well", duration: null, severity: "mild" },
  ]);
  // What the panel prints (description, duration, severity) is plain text, never JSON.
  for (const item of s) {
    for (const text of [item.description, item.duration, item.severity]) {
      if (text != null) assert.equal(/[{}"]/.test(text), false, text);
    }
  }
});

test("T9: legacy string symptoms and junk are handled", () => {
  assert.deepEqual(toSymptoms(["fever"]), [{ description: "Fever", duration: null, severity: null }]);
  assert.deepEqual(toSymptoms([{ duration: "2 days" }, null, ""]), []);
});

test("'none' and 'declined' display naturally", () => {
  assert.equal(displayValue("none"), "None");
  assert.equal(displayValue(["none"]), "None");
  assert.equal(displayValue("declined"), "Declined");
});

test("lists display as items; objects are never stringified", () => {
  assert.deepEqual(displayValue(["metformin", "amlodipine"]), ["Metformin", "Amlodipine"]);
  assert.equal(displayValue({ description: "cold" }), null);
  assert.deepEqual(displayValue([{ description: "cold", duration: "3 days" }]), ["Cold"]);
});

test("empty values render nothing (no broken placeholders)", () => {
  assert.equal(displayValue(""), null);
  assert.equal(displayValue(null), null);
  assert.equal(displayValue([]), null);
});
