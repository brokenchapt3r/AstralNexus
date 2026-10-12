import {describe, expect, it} from 'vitest'
import {supportsCapability} from '../app/capabilities.js'

describe('host capability advertisement', () => {
  it('honors explicit unsupported features without inventing support', () => {
    const info = {model: 'model', tools: {}, skills: {}, capabilities: {images: false, clipboard_images: false, voice: false, pets: false}}
    expect(supportsCapability('images', info)).toBe(false)
    expect(supportsCapability('voice', info)).toBe(false)
    expect(supportsCapability('pets', info)).toBe(false)
  })
})
