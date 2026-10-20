/* Tests for the profile-setup helpers (chip parsing/serialization,
   form-state mapping, wizard gating) and the shared picture rules.

   Run with `npm test`. No extra dependency: these cover the pure logic
   extracted out of the React components precisely so it could be tested
   without a DOM — the wizard and the edit form both build on it. */

import test from "node:test";
import assert from "node:assert/strict";

import {
  SKILL_SUGGESTIONS,
  addSkillChip,
  clearSetupDraft,
  isProfileComplete,
  isSetupReady,
  loadSetupDraft,
  parseSkillChips,
  saveSetupDraft,
  setupDraftKey,
  skillChipsToString,
  toFormState,
} from "../src/profile.js";
import { MAX_PICTURE_BYTES, pictureError } from "../src/lib/picture.js";

/* --- chip parsing: the comma string is the storage format ---------- */

test("parseSkillChips trims and drops empty segments", () => {
  assert.deepEqual(parseSkillChips("  Python ,  ,React "), [
    "Python",
    "React",
  ]);
});

test("parseSkillChips de-duplicates case-insensitively, keeping the first spelling", () => {
  assert.deepEqual(parseSkillChips("React, react, REACT, Node.js"), [
    "React",
    "Node.js",
  ]);
});

test("parseSkillChips treats null, undefined and empty as no chips", () => {
  assert.deepEqual(parseSkillChips(null), []);
  assert.deepEqual(parseSkillChips(undefined), []);
  assert.deepEqual(parseSkillChips(""), []);
  assert.deepEqual(parseSkillChips("   "), []);
});

test("skillChipsToString round-trips a clean comma string", () => {
  assert.equal(skillChipsToString(["Python", "React"]), "Python, React");
  assert.equal(skillChipsToString(parseSkillChips("Python ,React")), "Python, React");
});

test("skillChipsToString also collapses duplicates passed in as an array", () => {
  assert.equal(skillChipsToString(["React", "react"]), "React");
});

/* --- adding chips: the input may itself contain commas ------------- */

test("addSkillChip appends new chips and splits typed commas", () => {
  assert.deepEqual(addSkillChip(["Python"], "React, SQL"), [
    "Python",
    "React",
    "SQL",
  ]);
});

test("addSkillChip ignores a duplicate in any casing", () => {
  assert.deepEqual(addSkillChip(["Python"], "python"), ["Python"]);
});

test("addSkillChip reports no-ops by keeping the length unchanged", () => {
  const chips = ["Python"];
  assert.equal(addSkillChip(chips, "Python").length, chips.length);
  assert.equal(addSkillChip(chips, " , , ").length, chips.length);
});

/* --- form state: one mapping for wizard and edit form -------------- */

const SAMPLE_PROFILE = {
  id: 7,
  name: "Sara Khan",
  university_id: "2021-1234",
  email: "2021-1234@iqra.edu.pk",
  program: "BSCS",
  bio: "Hello",
  github: "https://github.com/sara",
  skills: "Python, React",
  interests: "",
  fyp_status: "Looking for a team",
  profile_completed: true,
};

test("toFormState copies every editable field", () => {
  assert.deepEqual(toFormState(SAMPLE_PROFILE), {
    name: "Sara Khan",
    program: "BSCS",
    profile_picture: "",
    bio: "Hello",
    github: "https://github.com/sara",
    linkedin: "",
    whatsapp: "",
    skills: "Python, React",
    interests: "",
    fyp_status: "Looking for a team",
  });
});

test("toFormState defaults missing fields to empty strings, never undefined", () => {
  const form = toFormState({ name: "Ali" });
  for (const value of Object.values(form)) assert.equal(typeof value, "string");
});

test("toFormState survives a null profile", () => {
  assert.deepEqual(toFormState(null).skills, "");
  assert.deepEqual(toFormState(undefined).name, "");
});

/* --- wizard gating: the same two required fields as completion ----- */

test("isSetupReady needs a status and a non-blank skill", () => {
  assert.equal(isSetupReady({ fyp_status: "Looking for a team", skills: "" }), false);
  assert.equal(isSetupReady({ fyp_status: "", skills: "Python" }), false);
  assert.equal(isSetupReady({ fyp_status: "Looking for a team", skills: "   " }), false);
  assert.equal(isSetupReady({ fyp_status: "Looking for a team", skills: "Python" }), true);
});

test("isSetupReady mirrors the isProfileComplete field fallback", () => {
  const partial = { fyp_status: "Already in a team", skills: "C++" };
  assert.equal(isSetupReady(partial), isProfileComplete(partial));
});

test("the backend completion flag still wins over missing fields", () => {
  assert.equal(isProfileComplete({ profile_completed: true }), true);
});

test("SKILL_SUGGESTIONS are unique, trimmed, and non-empty", () => {
  const lowered = SKILL_SUGGESTIONS.map((s) => s.toLowerCase());
  assert.equal(new Set(lowered).size, SKILL_SUGGESTIONS.length);

  for (const suggestion of SKILL_SUGGESTIONS) {
    assert.equal(suggestion, suggestion.trim());
    assert.ok(suggestion.length > 0);
  }
});

/* --- setup draft: progress survives a remount ---------------------- */

test("setupDraftKey is per-student and never empty", () => {
  assert.equal(setupDraftKey(7), "profile-setup-draft:7");
  assert.notEqual(setupDraftKey(7), setupDraftKey(8));
  assert.ok(setupDraftKey(undefined));
});

/* node has no sessionStorage: every helper must no-op, not throw —
   this is also what keeps them safe in private-browsing modes. */
test("draft helpers no-op where sessionStorage is unavailable", () => {
  assert.equal(loadSetupDraft(7), null);
  assert.equal(saveSetupDraft(7, { step: 1, form: {} }), undefined);
  assert.equal(clearSetupDraft(7), undefined);
});

/* --- picture rules mirror the backend's _validate_picture ----------- */

test("pictureError accepts the raster allow-list", () => {
  for (const type of ["image/jpeg", "image/png", "image/gif", "image/webp"]) {
    assert.equal(pictureError({ type, size: 1000 }), null);
  }
});

test("pictureError rejects non-raster files (no SVG)", () => {
  assert.match(pictureError({ type: "image/svg+xml", size: 1000 }), /JPEG/);
  assert.match(pictureError({ type: "application/pdf", size: 10 }), /JPEG/);
});

test("pictureError rejects files over the 1 MB cap", () => {
  assert.match(
    pictureError({ type: "image/png", size: MAX_PICTURE_BYTES + 1 }),
    /too large/
  );
  assert.equal(
    pictureError({ type: "image/png", size: MAX_PICTURE_BYTES }),
    null
  );
});

test("pictureError explains a missing file instead of throwing", () => {
  assert.match(pictureError(null), /choose an image/);
});
