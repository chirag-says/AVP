// node --test src/intake/*.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DEFAULT_LANGUAGE, LANGUAGES, languageFor, type UiText } from "./languages.ts";

test("the screen offers exactly the languages the server speaks", () => {
  // The server silently treats an unknown code as English, so a mismatch
  // would start a Kannada patient's session in English without any error.
  const server = readFileSync(new URL("../../../server/intake/languages.py", import.meta.url), "utf8");
  const serverCodes = [...server.matchAll(/code="([a-z]{2}-IN)"/g)].map((m) => m[1]).sort();
  assert.deepEqual(LANGUAGES.map((l) => l.code).sort(), serverCodes);
});

test("every language has every patient-facing string", () => {
  const keys = Object.keys(languageFor(DEFAULT_LANGUAGE).ui) as (keyof UiText)[];
  for (const lang of LANGUAGES) {
    for (const key of keys) {
      const value = lang.ui[key];
      const text = typeof value === "function" ? value("Ravi") : value;
      assert.ok(text.trim().length > 0, `${lang.code}.${key} is empty`);
    }
    assert.ok(lang.ui.thankYou("Ravi").includes("Ravi"), `${lang.code} thank-you drops the name`);
    assert.ok(!lang.ui.thankYou(null).includes("null"), `${lang.code} thank-you prints null`);
  }
});

test("English is the default and the fallback", () => {
  assert.equal(DEFAULT_LANGUAGE, "en-IN");
  assert.equal(languageFor("xx-XX").code, "en-IN");
  assert.equal(languageFor("kn-IN").label, "ಕನ್ನಡ");
});

test("English screen text is unchanged", () => {
  const en = languageFor("en-IN").ui;
  assert.equal(en.completeTitle, "Intake Complete");
  assert.equal(en.thankYou("Test"), "Thank you, Test.");
  assert.equal(en.recorded, "Your information has been successfully recorded.");
  assert.equal(en.tapToStart, "Tap to start");
});
