import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createSlashHandler } from '../app/createSlashHandler.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'

const buildContext = (canon: Record<string, string> = {}) => {
  const request = vi.fn(() => Promise.resolve({ ok: true, output: 'gateway result' }))
  const rpc = vi.fn(() => Promise.resolve({}))
  const context = {
    composer: {
      hasSelection: false,
      openEditor: vi.fn(),
      paste: vi.fn(),
      selection: { copySelection: vi.fn() },
      setInput: vi.fn()
    },
    gateway: {
      gw: { getLogTail: vi.fn(() => ''), request },
      rpc
    },
    local: {
      catalog: {
        canon,
        categories: [{ name: 'Runtime', pairs: Object.keys(canon).map(name => [name, 'runtime']) }],
        pairs: Object.keys(canon).map(name => [name, 'runtime']),
        skillCount: 0,
        sub: {}
      },
      getHistoryItems: vi.fn(() => []),
      setCatalog: vi.fn()
    },
    session: {
      die: vi.fn(),
      dieWithCode: vi.fn()
    },
    slashFlightRef: { current: 0 },
    transcript: {
      page: vi.fn(),
      panel: vi.fn(),
      send: vi.fn(),
      sys: vi.fn()
    },
    ui: {
      compact: false,
      detailsMode: 'collapsed',
      mouseTracking: 'buttons',
      sections: {},
      theme: { brand: { helpHeader: '天枢 / AstralNexus' } }
    }
  }

  return { context: context as any, request, rpc }
}

describe('AstralNexus slash boundary', () => {
  beforeEach(() => {
    resetUiState()
    patchUiState({ sid: 'astral-session' })
  })

  it('keeps presentation-only help inside Ink', () => {
    const { context, request } = buildContext({ '/help': '/help' })

    expect(createSlashHandler(context)('/help')).toBe(true)
    expect(context.transcript.panel).toHaveBeenCalled()
    expect(request).not.toHaveBeenCalled()
  })

  it('keeps terminal exit inside Ink', () => {
    const { context, request } = buildContext({ '/quit': '/quit' })

    expect(createSlashHandler(context)('/quit')).toBe(true)
    expect(context.session.die).toHaveBeenCalledOnce()
    expect(request).not.toHaveBeenCalled()
  })

  it.each(['/status', '/new build an app', '/tools list', '/skills list'])(
    'routes runtime command %s through the Gateway',
    async command => {
      const canonical = `/${command.slice(1).split(/\s+/, 1)[0]}`
      const { context, request, rpc } = buildContext({ [canonical]: canonical })

      expect(createSlashHandler(context)(command)).toBe(true)
      await vi.waitFor(() => {
        expect(request).toHaveBeenCalledWith('slash.exec', {
          command: command.slice(1),
          session_id: 'astral-session'
        })
      })
      expect(rpc).not.toHaveBeenCalled()
    }
  )

  it('does not revive an unregistered AstralNexus command locally', async () => {
    const { context, request } = buildContext()

    expect(createSlashHandler(context)('/topup')).toBe(true)
    await vi.waitFor(() => {
      expect(request).toHaveBeenCalledWith('slash.exec', {
        command: 'topup',
        session_id: 'astral-session'
      })
    })
  })
})
