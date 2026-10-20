import { useState } from "react";
import { Plus, X } from "lucide-react";
import { addSkillChip } from "../profile";

/* Tap-to-add / type-and-Enter chip editor for skills and interests.
   The stored value stays a comma-separated string (the API contract);
   chips are only the editing surface, so what teammates see on the
   public profile is unchanged. */
function ChipInput({
  id,
  chips,
  onChange,
  suggestions = [],
  placeholder = "Type your own and press Enter",
  inputLabel,
}) {
  const [draft, setDraft] = useState("");

  const commit = (raw) => {
    const next = addSkillChip(chips, raw);
    setDraft("");
    if (next.length !== chips.length) onChange(next);
  };

  const handleKeyDown = (e) => {
    // Enter adds; a comma mid-word commits what's typed so the field
    // behaves like the comma-separated text input it replaces.
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      commit(draft);
    }
  };

  const selected = new Set(chips.map((chip) => chip.toLowerCase()));
  const remaining = suggestions.filter(
    (suggestion) => !selected.has(suggestion.toLowerCase())
  );

  return (
    <div>
      {chips.length > 0 && (
        <ul className="mb-2.5 flex flex-wrap gap-1.5">
          {chips.map((chip) => (
            <li key={chip}>
              <button
                type="button"
                onClick={() => onChange(chips.filter((c) => c !== chip))}
                aria-label={`Remove ${chip}`}
                className="tag tag-mint inline-flex cursor-pointer items-center gap-1.5"
              >
                {chip}
                <X size={13} aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      )}

      <input
        id={id}
        type="text"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        aria-label={inputLabel}
        className="input-field"
      />

      {suggestions.length > 0 && (
        <div className="mt-3">
          <p className="mb-2 text-xs font-medium text-ink-muted">
            {chips.length === 0
              ? "Popular with classmates — tap to add:"
              : "Add more:"}
          </p>

          <div className="flex flex-wrap gap-1.5">
            {remaining.map((suggestion) => (
              <button
                key={suggestion}
                type="button"
                onClick={() => commit(suggestion)}
                className="tag cursor-pointer transition-colors hover:border-forest-700/40 hover:bg-forest-50"
              >
                <Plus size={13} className="mr-0.5" aria-hidden="true" />
                {suggestion}
              </button>
            ))}

            {remaining.length === 0 && (
              <p className="text-xs text-ink-muted">
                Every suggestion added — nice.
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default ChipInput;
