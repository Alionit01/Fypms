/* Avatar file rules, mirrored from backend/app/schemas.py
   (_validate_picture): the same raster allow-list (no SVG — it is
   markup, not an image) and the same 1 MB cap. Checking here turns a
   server-side 422 into an immediate, readable message. Shared by the
   setup wizard and the profile edit form. */

export const ACCEPTED_PICTURE_TYPES = [
  "image/jpeg",
  "image/png",
  "image/gif",
  "image/webp",
];

export const MAX_PICTURE_BYTES = 1024 * 1024;

/* Returns a readable problem description, or null when the file is
   fine. Pure — takes a {type, size}-shaped object — so the rules are
   unit-testable without a DOM. */
export function pictureError(file) {
  if (!file) return "Please choose an image first.";

  if (!ACCEPTED_PICTURE_TYPES.includes(file.type)) {
    return "Please choose a JPEG, PNG, GIF or WebP image.";
  }

  if (file.size > MAX_PICTURE_BYTES) {
    return "Image is too large. Please use a file under 1 MB.";
  }

  return null;
}

/* Wire a file input's selection into the form state: reject with a
   message, or read the image as a data URL into form.profile_picture. */
export function applyPictureSelection(file, setForm, setError) {
  if (!file) return;

  const problem = pictureError(file);

  if (problem) {
    setError(problem);
    return;
  }

  setError("");

  const reader = new FileReader();

  reader.onload = () => {
    setForm((current) => ({
      ...current,
      profile_picture: reader.result,
    }));
  };

  reader.readAsDataURL(file);
}
