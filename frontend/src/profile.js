// Single source of truth for "is this profile complete?" and the pure
// helpers the profile forms are built on. Everything here is
// deliberately framework-free so `tests/profile.test.js` can cover it
// without a DOM.
// The backend flag is authoritative (grandfathered users keep it TRUE);
// the field check is a fallback so a stale flag cannot lock anyone out.
// A profile picture is optional — skills and FYP status are required.
export function isProfileComplete(profile) {
  if (!profile) return false;
  if (profile.profile_completed === true) return true;
  return Boolean(
    profile.skills && profile.skills.trim() && profile.fyp_status
  );
}

export function missingProfileFields(profile) {
  const missing = [];
  if (!profile) return ["Skills", "FYP status"];
  if (!profile.skills || !profile.skills.trim()) missing.push("Skills");
  if (!profile.fyp_status) missing.push("FYP status");
  return missing;
}

/* One-tap skill suggestions for the setup wizard. Skills are stored as
   a comma-separated string and the student directory matches a typed
   search with a case-insensitive substring `LIKE` (see
   backend/app/services/directory.py::_search_pattern), so single,
   plainly-spelled terms are what makes a profile findable: a classmate
   searching "react" matches a chip stored as "React". */
export const SKILL_SUGGESTIONS = [
  "Python",
  "JavaScript",
  "React",
  "Node.js",
  "Java",
  "C++",
  "SQL",
  "MongoDB",
  "Machine Learning",
  "Data Analysis",
  "UI/UX",
  "Figma",
  "Cybersecurity",
  "Flutter",
  "AWS",
  "Embedded Systems",
];

/* The comma-separated string on the API <-> list of chips in the UI.
   Parsing trims, drops empties, and de-duplicates case-insensitively
   (keeping the first spelling) so "React, react" cannot happen. */
export function parseSkillChips(value) {
  const chips = [];
  const seen = new Set();

  for (const part of String(value || "").split(",")) {
    const chip = part.trim();
    const key = chip.toLowerCase();

    if (!chip || seen.has(key)) continue;

    seen.add(key);
    chips.push(chip);
  }

  return chips;
}

export function skillChipsToString(chips) {
  return parseSkillChips(
    Array.isArray(chips) ? chips.join(", ") : chips
  ).join(", ");
}

/* Append what was typed (possibly several comma-separated words) to
   the current chips; duplicates are ignored so the caller can use the
   length change as "did anything actually get added?". */
export function addSkillChip(chips, input) {
  const current = parseSkillChips(
    Array.isArray(chips) ? chips.join(", ") : chips
  );
  const seen = new Set(current.map((chip) => chip.toLowerCase()));

  for (const chip of parseSkillChips(input)) {
    if (seen.has(chip.toLowerCase())) continue;

    seen.add(chip.toLowerCase());
    current.push(chip);
  }

  return current;
}

/* The profile row <-> form-state mapping used by both the setup
   wizard and the edit form. Backend contract note: `PUT /my-profile`
   requires `name` and `program` (schemas.StudentUpdate), so every save
   must send the whole form — never a partial patch. */
export function toFormState(profile) {
  const data = profile || {};

  return {
    name: data.name || "",
    program: data.program || "",
    profile_picture: data.profile_picture || "",
    bio: data.bio || "",
    github: data.github || "",
    linkedin: data.linkedin || "",
    whatsapp: data.whatsapp || "",
    skills: data.skills || "",
    interests: data.interests || "",
    fyp_status: data.fyp_status || "",
  };
}

/* The wizard's step gating, mirroring isProfileComplete's field check
   on the in-progress form: the two required fields and nothing else. */
export function isSetupReady(form) {
  return Boolean(
    form && form.fyp_status && form.skills && form.skills.trim()
  );
}

/* --- setup draft ---------------------------------------------------
   An in-progress setup survives a remount: bouncing through a
   protected page (or a stray reload) unmounts the wizard, and redoing
   two taps from scratch is exactly the friction setup is meant to
   avoid. The draft is per-tab and per-student, and is cleared the
   moment the real profile save succeeds. All three helpers no-op
   safely where sessionStorage is unavailable (private mode, tests). */

export function setupDraftKey(studentId) {
  return `profile-setup-draft:${studentId || "unknown"}`;
}

export function loadSetupDraft(studentId) {
  if (typeof sessionStorage === "undefined") return null;

  try {
    const raw = sessionStorage.getItem(setupDraftKey(studentId));
    const draft = raw ? JSON.parse(raw) : null;

    if (!draft || typeof draft !== "object" || !draft.form) return null;
    if (!(draft.step >= 1 && draft.step <= 3)) return null;

    return draft;
  } catch {
    return null;
  }
}

export function saveSetupDraft(studentId, draft) {
  if (typeof sessionStorage === "undefined") return;

  try {
    sessionStorage.setItem(
      setupDraftKey(studentId),
      JSON.stringify(draft)
    );
  } catch {
    // Quota or private-mode failure: losing the draft is acceptable.
  }
}

export function clearSetupDraft(studentId) {
  if (typeof sessionStorage === "undefined") return;

  try {
    sessionStorage.removeItem(setupDraftKey(studentId));
  } catch {
    // Nothing to recover from — the draft is an optimisation only.
  }
}
