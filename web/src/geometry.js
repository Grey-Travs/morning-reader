// The overlay's coordinate maths — the browser half of one rule implemented twice.
//
// `morning/pageread.py` stores every region's box as FRACTIONS of the page, so a page
// re-scanned at another resolution keeps its positions. The overlay has to put those
// fractions back onto a rendered image, and the server has to be able to check a box
// a human moved. That is two implementations of one rule, in two languages, and two
// implementations of one rule drift.
//
// `tests/test_geometry_parity.py` shells out to Node and compares this file against
// `morning/pageread.py` case by case. It also asserts the RIGHT answers, not merely
// agreement — the reference app has a parity test that passed for months while the two
// sides genuinely disagreed, because both were returning their input unchanged.
//
// Keep this file free of imports and of anything browser-specific. The parity harness
// loads it in bare Node, where there is no DOM.

// Float slack, mirroring `pageread._EPS`. A model that answers in pixels and a box
// that ends exactly at the right edge produce 1.0000000000000002 often enough that an
// exact comparison rejects legitimate boxes.
export const EPS = 1e-6

// Python's `float()` accepts a numeric string and a bool (`float(True)` is 1.0) and
// raises on everything else. This mirrors that exactly rather than using `Number()`,
// which would quietly turn null and '' into 0 and disagree with the server about
// whether a box is valid.
function asFloat(value) {
  if (typeof value === 'boolean') return value ? 1 : 0
  if (typeof value === 'number') return value
  if (typeof value === 'string') {
    const trimmed = value.trim()
    if (trimmed === '') return null
    const parsed = Number(trimmed)
    return Number.isNaN(parsed) && trimmed.toLowerCase() !== 'nan' ? null : parsed
  }
  return null
}

/**
 * A stored fractional box placed on an image of the given size.
 *
 * The inverse of `fromPixels`, and what the overlay draws with. Mirrors
 * `pageread.to_pixels`.
 */
export function toPixels(box, width, height) {
  const [x, y, w, h] = box
  return [x * width, y * height, w * width, h * height]
}

/**
 * A pixel box converted to the stored fractional one. Mirrors `pageread.from_pixels`.
 *
 * Zero dimensions yield a zero box rather than throwing: a page whose dimensions could
 * not be determined must still be able to store its text.
 */
export function fromPixels(x, y, w, h, width, height) {
  if (width <= 0 || height <= 0) return [0, 0, 0, 0]
  return [x / width, y / height, w / width, h / height]
}

/**
 * Whether a box is a well-formed fraction box that fits on the page.
 * Mirrors `pageread.box_is_inside`, coercion rules included.
 */
export function boxIsInside(box) {
  if (!Array.isArray(box) || box.length !== 4) return false
  const values = box.map(asFloat)
  if (values.some((v) => v === null)) return false
  const [x, y, w, h] = values
  // NaN compares false against everything, so it has to be rejected by name.
  if (values.some((v) => Number.isNaN(v))) return false
  if (w < 0 || h < 0) return false
  return x >= -EPS && y >= -EPS && x + w <= 1 + EPS && y + h <= 1 + EPS
}

/**
 * A stored box as CSS percentages, which is how the overlay actually positions things.
 *
 * Percentages rather than pixels on purpose: the box then tracks the image through
 * every resize, every zoom and every device pixel ratio without a resize listener and
 * without a re-render. There is no server-side counterpart, so this one is pinned by
 * `geometry.test.js` alone.
 */
export function toPercent(box) {
  const [x, y, w, h] = box
  return {
    left: `${x * 100}%`,
    top: `${y * 100}%`,
    width: `${w * 100}%`,
    height: `${h * 100}%`,
  }
}
