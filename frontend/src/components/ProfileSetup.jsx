import { useEffect, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Camera,
  CheckCircle2,
  Save,
  Users,
} from "lucide-react";
import API_URL from "../api";
import { extractErrorMessage } from "../api/errors";
import {
  SKILL_SUGGESTIONS,
  clearSetupDraft,
  isSetupReady,
  loadSetupDraft,
  parseSkillChips,
  saveSetupDraft,
  skillChipsToString,
  toFormState,
} from "../profile";
import { applyPictureSelection } from "../lib/picture";
import Alert from "./Alert";
import Avatar from "./Avatar";
import ChipInput from "./ChipInput";

/* The two FYP statuses, exactly as the API stores them
   (backend FYP_STATUSES). Cards instead of a dropdown with an empty
   default: one tap, and the choice explains itself. */
const STATUS_OPTIONS = [
  {
    value: "Looking for a team",
    title: "Looking for a team",
    blurb: "Classmates who need a member can find you and invite you.",
    Icon: Users,
  },
  {
    value: "Already in a team",
    title: "Already in a team",
    blurb: "Your card shows you as unavailable, so fewer people reach out.",
    Icon: CheckCircle2,
  },
];

const TOTAL_STEPS = 3;

function firstName(name) {
  return String(name || "").trim().split(/\s+/)[0] || "";
}

/* First-run setup for an incomplete profile: two required questions
   one screen at a time, then the optional extras with an explicit way
   out. Replaces the old "full edit form + surprise validation" flow;
   the save is a single PUT with the same fields as before, so the API
   contract is untouched. Renders nothing for complete profiles —
   MyProfile only mounts this when isProfileComplete() is false. */
function ProfileSetup({ profile, token, onUpdated, onDone }) {
  // Restore any half-finished draft (the wizard unmounts when a
  // protected page bounces the student back here, or on reload).
  const [step, setStep] = useState(() => loadSetupDraft(profile.id)?.step || 1);
  const [form, setForm] = useState(() => {
    const draft = loadSetupDraft(profile.id);
    return draft
      ? { ...toFormState(profile), ...draft.form }
      : toFormState(profile);
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  // Keep the draft current while the student works through the steps.
  useEffect(() => {
    if (step <= 3) saveSetupDraft(profile.id, { step, form });
  }, [profile.id, step, form]);

  const goTo = (nextStep) => {
    setError("");
    setStep(nextStep);
  };

  const handleChange = (e) => {
    setForm({ ...form, [e.target.name]: e.target.value });
  };

  const handlePicture = (e) => {
    applyPictureSelection(e.target.files?.[0], setForm, setError);
  };

  const handleSubmit = async () => {
    // The step flow already guarantees these, but never save a profile
    // the backend would not complete.
    if (!isSetupReady(form)) {
      setError("Please pick a status and add at least one skill first.");
      return;
    }

    setSaving(true);
    setError("");

    try {
      const response = await fetch(`${API_URL}/my-profile`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        // name/program are required by the update schema, so always
        // send the full seeded form — never a partial patch.
        body: JSON.stringify(form),
      });

      const data = await response.json();

      if (!response.ok) {
        setError(
          extractErrorMessage(response, data, "Could not save your profile.")
        );
        return;
      }

      onUpdated(data.profile);
      clearSetupDraft(profile.id);
      setStep(4);
    } catch {
      setError("Could not connect to the server.");
    } finally {
      setSaving(false);
    }
  };

  const skills = parseSkillChips(form.skills);
  const interests = parseSkillChips(form.interests);

  /* --- Step 4: done ------------------------------------------------ */
  if (step === 4) {
    return (
      <main className="page-container max-w-3xl">
        <section className="card p-8 text-center">
          <CheckCircle2
            size={44}
            className="mx-auto text-success-700"
            aria-hidden="true"
          />

          <h1 className="display mt-5 text-4xl">
            You're <em className="accent-italic">all set.</em>
          </h1>

          <p className="mt-4 text-ink-soft leading-relaxed">
            Your profile is live — classmates can find you, and you can
            create or join teams.
          </p>

          <button
            type="button"
            onClick={onDone}
            className="primary-button mt-7 !px-6 !py-3"
          >
            Start exploring
            <ArrowRight size={16} aria-hidden="true" />
          </button>
        </section>
      </main>
    );
  }

  /* --- Steps 1-3 -------------------------------------------------- */
  return (
    <main className="page-container max-w-3xl">
      <div className="mb-8">
        <p className="eyebrow">Account setup</p>

        <h1 className="display mt-3 text-4xl sm:text-5xl">
          Set up your <em className="accent-italic">profile.</em>
        </h1>

        <p className="mt-4 text-ink-soft leading-relaxed">
          {firstName(profile.name)
            ? `Two quick questions, ${firstName(profile.name)} — this is what teammates see.`
            : "Two quick questions — this is what teammates see."}
        </p>
      </div>

      <div className="mb-6">
        <p className="text-xs font-semibold uppercase tracking-[0.14em] text-ink-muted">
          Step {step} of {TOTAL_STEPS}
        </p>

        <div className="mt-2.5 flex gap-1.5" aria-hidden="true">
          {[1, 2, 3].map((segment) => (
            <span
              key={segment}
              className={`h-1.5 flex-1 rounded-full ${
                segment <= step ? "bg-forest-700" : "bg-forest-900/10"
              }`}
            />
          ))}
        </div>
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (step === 1) {
            if (form.fyp_status) goTo(2);
          } else if (step === 2) {
            if (isSetupReady(form)) goTo(3);
          } else {
            handleSubmit();
          }
        }}
        className="card p-5 sm:p-8"
      >
        {step === 1 && (
          <fieldset>
            <legend className="display text-xl text-forest-900">
              Are you looking for a team right now?
            </legend>

            <p className="mt-2 text-sm text-ink-soft">
              This becomes the badge on your card in the student
              directory.
            </p>

            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              {STATUS_OPTIONS.map((option) => {
                const selected = form.fyp_status === option.value;

                return (
                  <label
                    key={option.value}
                    className={`cursor-pointer rounded-2xl border p-5 transition-colors has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-forest-600/40 ${
                      selected
                        ? "border-forest-700 bg-cream-100 ring-2 ring-forest-600/25"
                        : "border-forest-900/15 bg-paper hover:border-forest-900/30"
                    }`}
                  >
                    <input
                      type="radio"
                      name="fyp_status"
                      value={option.value}
                      checked={selected}
                      onChange={handleChange}
                      className="sr-only"
                    />

                    <span className="flex items-center justify-between gap-2">
                      <span className="font-semibold text-forest-900">
                        {option.title}
                      </span>

                      {selected && (
                        <CheckCircle2
                          size={18}
                          className="shrink-0 text-forest-700"
                          aria-hidden="true"
                        />
                      )}
                    </span>

                    <span className="mt-1.5 block text-sm text-ink-soft leading-relaxed">
                      {option.blurb}
                    </span>
                  </label>
                );
              })}
            </div>
          </fieldset>
        )}

        {step === 2 && (
          <section>
            <h2 className="display text-xl text-forest-900">
              What can you do?
            </h2>

            <p className="mt-2 text-sm text-ink-soft">
              Your skills are how classmates find you in the directory.
              Add at least one — tap a suggestion or type your own.
            </p>

            <div className="mt-4">
              <ChipInput
                id="setup-skills"
                chips={skills}
                onChange={(chips) =>
                  setForm({ ...form, skills: skillChipsToString(chips) })
                }
                suggestions={SKILL_SUGGESTIONS}
                inputLabel="Your skills"
                placeholder="e.g. React, Python, UI/UX"
              />
            </div>
          </section>
        )}

        {step === 3 && (
          <section>
            <h2 className="display text-xl text-forest-900">
              Anything else? All optional.
            </h2>

            <p className="mt-2 text-sm text-ink-soft">
              A photo, a line about you, and links help teammates trust
              you — but you can skip this and add it later from Edit
              Profile.
            </p>

            <div className="mt-5 space-y-5">
              <Avatar
                name={form.name}
                src={form.profile_picture}
                size="lg"
              />

              <div>
                <label
                  htmlFor="setup_picture_upload"
                  className="block text-sm font-medium text-ink-soft mb-2"
                >
                  Profile picture
                </label>

                <input
                  id="setup_picture_upload"
                  type="file"
                  accept="image/*"
                  onChange={handlePicture}
                  className="input-file"
                />

                <p className="text-xs text-ink-muted mt-2 inline-flex items-center gap-1.5">
                  <Camera size={13} aria-hidden="true" />
                  JPEG, PNG, GIF or WebP, up to 1 MB.
                </p>
              </div>

              <div>
                <label
                  htmlFor="setup_bio"
                  className="block text-sm font-medium text-ink-soft mb-2"
                >
                  Bio
                </label>

                <textarea
                  id="setup_bio"
                  name="bio"
                  value={form.bio}
                  onChange={handleChange}
                  rows="3"
                  placeholder="One line about you and your FYP idea..."
                  className="input-field resize-none"
                />
              </div>

              <div>
                <label
                  htmlFor="setup_interests"
                  className="block text-sm font-medium text-ink-soft mb-2"
                >
                  Interests
                </label>

                <ChipInput
                  id="setup_interests"
                  chips={interests}
                  onChange={(chips) =>
                    setForm({ ...form, interests: skillChipsToString(chips) })
                  }
                  inputLabel="Your interests"
                  placeholder="e.g. AI, Web Development, Cybersecurity"
                />
              </div>

              <div className="grid gap-5 sm:grid-cols-2">
                <div>
                  <label
                    htmlFor="setup_github"
                    className="block text-sm font-medium text-ink-soft mb-2"
                  >
                    GitHub
                  </label>

                  <input
                    id="setup_github"
                    type="url"
                    name="github"
                    value={form.github}
                    onChange={handleChange}
                    placeholder="https://github.com/username"
                    className="input-field"
                  />
                </div>

                <div>
                  <label
                    htmlFor="setup_linkedin"
                    className="block text-sm font-medium text-ink-soft mb-2"
                  >
                    LinkedIn
                  </label>

                  <input
                    id="setup_linkedin"
                    type="url"
                    name="linkedin"
                    value={form.linkedin}
                    onChange={handleChange}
                    placeholder="https://linkedin.com/in/username"
                    className="input-field"
                  />
                </div>
              </div>

              <div>
                <label
                  htmlFor="setup_whatsapp"
                  className="block text-sm font-medium text-ink-soft mb-2"
                >
                  WhatsApp
                </label>

                <input
                  id="setup_whatsapp"
                  type="text"
                  name="whatsapp"
                  value={form.whatsapp}
                  onChange={handleChange}
                  placeholder="Your WhatsApp number"
                  className="input-field"
                />

                <p className="text-xs text-ink-muted mt-2">
                  Your contact details are not shown publicly.
                </p>
              </div>
            </div>
          </section>
        )}

        {error && (
          <div className="mt-6">
            <Alert kind="error">{error}</Alert>
          </div>
        )}

        <div className="mt-8 pt-6 divider flex flex-col-reverse sm:flex-row sm:items-center sm:justify-between gap-3">
          {step === 1 ? (
            <span aria-hidden="true" className="hidden sm:block" />
          ) : (
            <button
              type="button"
              onClick={() => goTo(step - 1)}
              className="secondary-button"
            >
              <ArrowLeft size={16} aria-hidden="true" />
              Back
            </button>
          )}

          {step === 1 && (
            <button
              type="submit"
              disabled={!form.fyp_status}
              className="primary-button w-full sm:w-auto !px-6 !py-3"
            >
              Continue
              <ArrowRight size={16} aria-hidden="true" />
            </button>
          )}

          {step === 2 && (
            <button
              type="submit"
              disabled={skills.length === 0}
              className="primary-button w-full sm:w-auto !px-6 !py-3"
            >
              Continue
              <ArrowRight size={16} aria-hidden="true" />
            </button>
          )}

          {step === 3 && (
            <div className="flex flex-col-reverse gap-3 sm:flex-row sm:items-center">
              <button
                type="button"
                onClick={handleSubmit}
                disabled={saving}
                className="secondary-button"
              >
                Skip for now
              </button>

              <button
                type="submit"
                disabled={saving}
                className="primary-button w-full sm:w-auto !px-6 !py-3"
              >
                <Save size={16} aria-hidden="true" />
                {saving ? "Saving..." : "Save & finish"}
              </button>
            </div>
          )}
        </div>
      </form>
    </main>
  );
}

export default ProfileSetup;
