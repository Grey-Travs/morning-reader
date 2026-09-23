import { useEffect, useRef, useState } from 'react'
import { isDrawable, toPercent } from '../geometry'

// The page image with the English positioned over it. The original art is never
// modified — the English is a sibling layer over an unmodified <img>, and nothing in
// this app ever writes to projects/<pid>/pages/.
//
// Four things here are load-bearing, and three of them are silent when wrong:
//
//  * **No `object-fit` other than `fill`.** The thumbnails on the pages screen use
//    `cover`, and copying that here would letterbox or crop the picture INSIDE the
//    element — so percentages of the element stop being percentages of the picture,
//    and every box is offset and scaled with no error anywhere.
//  * **The wrapper's aspect ratio comes from the STORED dimensions**, set before the
//    image loads. Without it the wrapper has zero height for a frame and every box
//    stacks at the top.
//  * **Percentages, not pixels.** The box then tracks the image through every resize,
//    pinch-zoom and device pixel ratio with no listener and no re-render.
//  * **A box that cannot be drawn is reported, not skipped.** `isDrawable` mirrors
//    `pageread.is_drawable` and the server counts with the same rule, so the caller's
//    "N lines could not be placed" is true. Silently rendering nothing would show a
//    bubble with no English and no explanation.

// Below this width an overlay of English over manga art is unreadable however clever
// the fitting, so the honest answer is not to try: show numbered markers over clean
// art and let the line strip be the reading surface.
const NARROW = 560

function useNarrow() {
  const [narrow, setNarrow] = useState(false)
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return undefined
    const query = window.matchMedia(`(max-width: ${NARROW}px)`)
    const apply = () => setNarrow(query.matches)
    apply()
    query.addEventListener?.('change', apply)
    return () => query.removeEventListener?.('change', apply)
  }, [])
  return narrow
}

function Line({ region, index, selected, onSelect, markers }) {
  const ref = useRef(null)
  const [overflows, setOverflows] = useState(false)
  const english = (region.english || '').trim()

  // Measured once per render rather than guessed from a character count: what fits
  // depends on the box, the font and the viewport, none of which are known up front.
  useEffect(() => {
    const node = ref.current
    if (!node || markers) return
    setOverflows(node.scrollHeight > node.clientHeight + 1)
  }, [english, markers, region.box])

  const base = {
    position: 'absolute',
    ...toPercent(region.box),
    // Reading order is z-order, so overlapping bubbles are never ambiguous — and the
    // selected line is always on top of everything.
    zIndex: selected ? 500 : 100 + index,
  }

  if (markers) {
    const [x, y, w, h] = region.box
    return (
      <button
        type="button"
        aria-label={`Line ${index + 1}${region.speaker ? `, ${region.speaker}` : ''}`}
        onClick={() => onSelect(region.id)}
        style={{
          position: 'absolute',
          left: `${(x + w / 2) * 100}%`,
          top: `${(y + h / 2) * 100}%`,
          transform: 'translate(-50%, -50%)',
          zIndex: selected ? 500 : 100 + index,
          minWidth: '1.6rem', height: '1.6rem', borderRadius: '999px',
          border: selected ? '2px solid var(--accent)' : '1px solid var(--line-strong)',
          background: 'var(--surface)', color: 'var(--ink)',
          fontSize: '0.75rem', lineHeight: 1, cursor: 'pointer', padding: 0,
        }}
      >
        {index + 1}
      </button>
    )
  }

  return (
    <div
      ref={ref}
      role="button"
      tabIndex={0}
      aria-label={`Line ${index + 1}`}
      onClick={() => onSelect(region.id)}
      onKeyDown={(e) => { if (e.key === 'Enter') onSelect(region.id) }}
      style={{
        ...base,
        // The box ANCHORS the panel; it does not clip it. Nothing here deletes prose,
        // so when the English does not fit the element grows from the box's centre
        // above its neighbours rather than cutting the line off.
        ...(overflows && !selected ? { height: 'auto', minHeight: base.height } : {}),
        ...(selected ? { height: 'auto', minHeight: base.height } : {}),
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        padding: '0.15rem 0.25rem',
        borderRadius: '0.4rem',
        background: selected ? 'var(--surface)' : 'rgba(255,255,255,0.92)',
        color: '#111',
        outline: selected ? '2px solid var(--accent)' : 'none',
        border: region.stale ? '2px dashed var(--warn)' : '1px solid rgba(0,0,0,0.12)',
        fontSize: 'clamp(0.72rem, 1.4vw, 1rem)',
        lineHeight: 1.15,
        textAlign: 'center',
        overflow: 'visible',
        cursor: 'pointer',
      }}
    >
      {english || <span style={{ opacity: 0.55 }}>{region.text}</span>}
    </div>
  )
}

export default function PageOverlay({ pid, page, selected, onSelect, showOverlay = true }) {
  const narrow = useNarrow()
  const measured = page.width > 0 && page.height > 0

  // Numbered from the SAME list the line strip uses, not from the drawable subset.
  // On a phone the overlay is numbered markers and the strip is the reading surface,
  // so those numbers are the only link between the two — and the moment one region
  // came back with a box that could not be placed, every marker after it was one lower
  // than its line. Marker 3 on the art was line 4 in the strip, and the wrong number
  // went into the aria-label too.
  const translatable = (page.regions || []).filter((r) => r.translatable)
  const drawable = translatable.filter((r) => isDrawable(r.box))
  const undrawable = translatable.length - drawable.length

  return (
    <div>
      {!measured && (
        <div className="card mb-2 p-3 text-sm" style={{ borderColor: 'var(--warn)' }}>
          <p style={{ color: 'var(--warn)' }}>
            This page&rsquo;s size could not be read, so the English cannot be
            positioned on it.
          </p>
          <p className="mt-1 text-muted">
            Re-scan or re-export it and read it again. The lines below still work.
          </p>
        </div>
      )}

      <div
        style={{
          position: 'relative',
          overflow: 'hidden',
          // From the STORED dimensions, so the box positions are meaningful before the
          // image has loaded and there is no frame where everything stacks at the top.
          aspectRatio: measured ? `${page.width} / ${page.height}` : '2 / 3',
          background: 'var(--surface-2)',
          borderRadius: '0.5rem',
        }}
      >
        <img
          src={`/api/projects/${pid}/pages/${page.id}/image`}
          alt={page.name || `Page ${page.seq}`}
          style={{
            width: '100%', height: '100%', display: 'block',
            // `fill`, never `cover` or `contain`: either of those crops or letterboxes
            // the picture inside the element and every box drifts.
            objectFit: 'fill',
          }}
        />

        {showOverlay && measured && drawable.map((region) => (
          <Line
            key={region.id}
            region={region}
            index={translatable.indexOf(region)}
            markers={narrow}
            selected={selected === region.id}
            onSelect={onSelect}
          />
        ))}
      </div>

      {undrawable > 0 && (
        <p className="mt-2 text-xs" style={{ color: 'var(--warn)' }}>
          {undrawable} line{undrawable === 1 ? '' : 's'} could not be placed on this
          page — {undrawable === 1 ? 'its box has' : 'their boxes have'} no position.
          Read the page again, or use the lines below.
        </p>
      )}
    </div>
  )
}
