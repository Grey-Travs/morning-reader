import { describe, expect, it } from 'vitest'
import { EPS, boxIsInside, fromPixels, toPercent, toPixels } from './geometry'

// tests/test_geometry_parity.py already compares this file against morning/pageread.py
// case by case. This one exists so `npm test` catches a break without Node-shelling and
// without a Python environment — and so the browser-only part (`toPercent`, which has no
// server counterpart) is pinned from the side that actually uses it.

describe('placing a stored box on an image', () => {
  it('puts a quarter-across box a quarter across, at any size', () => {
    expect(toPixels([0.25, 0.5, 0.25, 0.125], 1600, 2400)).toEqual([400, 1200, 400, 300])
    expect(toPixels([0.25, 0.5, 0.25, 0.125], 800, 1200)).toEqual([200, 600, 200, 150])
  })

  it('round-trips pixels through fractions', () => {
    const box = fromPixels(160, 480, 480, 960, 1600, 2400)
    expect(toPixels(box, 1600, 2400)).toEqual([160, 480, 480, 960])
  })

  it('survives a re-scan at another resolution', () => {
    // The whole reason boxes are fractions: the same stored position lands on the same
    // part of the picture whatever it is rendered at.
    const box = fromPixels(400, 1200, 400, 300, 1600, 2400)
    expect(toPixels(box, 800, 1200)).toEqual([200, 600, 200, 150])
  })

  it('yields a zero box for a page whose size is unknown, rather than dividing by zero',
    () => {
      expect(fromPixels(100, 100, 100, 100, 0, 0)).toEqual([0, 0, 0, 0])
      expect(fromPixels(100, 100, 100, 100, 1600, 0)).toEqual([0, 0, 0, 0])
    })
})

describe('what counts as a box', () => {
  it('accepts the whole page and an ordinary bubble', () => {
    expect(boxIsInside([0, 0, 1, 1])).toBe(true)
    expect(boxIsInside([0.1, 0.2, 0.3, 0.4])).toBe(true)
  })

  it('accepts a box of nothing, because an unmeasured page still stores its text', () => {
    expect(boxIsInside([0, 0, 0, 0])).toBe(true)
  })

  it('rejects a box that runs off the page', () => {
    expect(boxIsInside([0.9, 0, 0.2, 1])).toBe(false)
  })

  it('admits a box that ends a hair past the edge, and not one that ends well past it',
    () => {
      // A model answering in pixels produces 1.0000000000000002 often enough that an
      // exact comparison would reject legitimate boxes.
      expect(boxIsInside([0, 0, 1.0000000000000002, 1])).toBe(true)
      expect(boxIsInside([0, 0, 1.01, 1])).toBe(false)
      expect(EPS).toBe(1e-6)
    })

  it('rejects a negative size', () => {
    expect(boxIsInside([0.5, 0.5, -0.1, 0.1])).toBe(false)
    expect(boxIsInside([0.5, 0.5, 0.1, -0.1])).toBe(false)
  })

  it('rejects NaN, which a range check alone lets straight through', () => {
    // NaN compares false against everything, so `x >= -EPS && x + w <= 1 + EPS` is
    // false and a naive `!inside` inversion would call it valid. The overlay would then
    // render at `NaN%`, which draws nothing and reports nothing.
    expect(boxIsInside([NaN, 0, 1, 1])).toBe(false)
    expect(boxIsInside([0, 0, NaN, 1])).toBe(false)
  })

  it('rejects infinity', () => {
    expect(boxIsInside([Infinity, 0, 0, 0])).toBe(false)
    expect(boxIsInside([0, 0, Infinity, 0])).toBe(false)
  })

  it('rejects null and the empty string, which Number() would turn into zero', () => {
    // This is the coercion the server does NOT do: Python's float(None) raises. A box
    // the browser accepted and the server refused would be a region on screen that
    // cannot be saved.
    expect(boxIsInside([null, 0, 0, 0])).toBe(false)
    expect(boxIsInside(['', 0, 0, 0])).toBe(false)
    expect(boxIsInside(['abc', 0, 0, 0])).toBe(false)
  })

  it('accepts numbers written as strings, which a JSON round-trip can produce', () => {
    expect(boxIsInside(['0.1', '0.2', '0.3', '0.4'])).toBe(true)
  })

  it('rejects anything that is not four values', () => {
    expect(boxIsInside([0.1, 0.1, 0.2])).toBe(false)
    expect(boxIsInside([0.1, 0.1, 0.2, 0.2, 0.3])).toBe(false)
    expect(boxIsInside(null)).toBe(false)
    expect(boxIsInside('0.1,0.1,0.2,0.2')).toBe(false)
  })
})

describe('what the overlay actually renders with', () => {
  it('gives percentages, so a box tracks the image through a resize', () => {
    expect(toPercent([0.25, 0.5, 0.125, 0.0625])).toEqual({
      left: '25%', top: '50%', width: '12.5%', height: '6.25%',
    })
  })

  it('keeps full precision rather than rounding to a whole percent', () => {
    // Rounding here would move a bubble by up to half a percent of the page — about
    // eight pixels on a 1600px scan, which is enough to sit over the wrong balloon on a
    // dense page.
    expect(toPercent([1 / 3, 0, 0, 0]).left).toBe(`${(1 / 3) * 100}%`)
  })
})
