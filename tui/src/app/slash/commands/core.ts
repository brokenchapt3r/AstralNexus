import { forceRedraw, type MouseTrackingMode } from '@astralnexus/ink'

import { DASHBOARD_TUI_MODE } from '../../../config/env.js'
import { HOTKEYS } from '../../../content/hotkeys.js'
import { isSectionName, nextDetailsMode, parseDetailsMode, SECTION_NAMES } from '../../../domain/details.js'
import type {
  ConfigGetValueResponse,
  ConfigSetResponse,
  SessionTitleResponse
} from '../../../gatewayTypes.js'
import { writeClipboardText } from '../../../lib/clipboard.js'
import { writeOsc52Clipboard } from '../../../lib/osc52.js'
import { configureDetectedTerminalKeybindings, configureTerminalKeybindings } from '../../../lib/terminalSetup.js'
import type { PanelSection } from '../../../types.js'
import type { StatusBarMode } from '../../interfaces.js'
import { patchUiState } from '../../uiStore.js'
import type { SlashCommand } from '../types.js'

const flagFromArg = (arg: string, current: boolean): boolean | null => {
  if (!arg) {
    return !current
  }

  const mode = arg.trim().toLowerCase()

  if (mode === 'on') {
    return true
  }

  if (mode === 'off') {
    return false
  }

  if (mode === 'toggle') {
    return !current
  }

  return null
}

// `/mouse` toggles between full tracking and off when called bare so the
// old binary muscle-memory still works. Explicit presets (wheel / buttons /
// all) target the tmux-friendly hover-free subsets.
const MOUSE_MODE_ALIASES: Record<string, MouseTrackingMode> = {
  all: 'all',
  any: 'all',
  button: 'buttons',
  buttons: 'buttons',
  click: 'buttons',
  full: 'all',
  off: 'off',
  on: 'all',
  scroll: 'wheel',
  wheel: 'wheel'
}

const mouseModeFromArg = (arg: string, current: MouseTrackingMode): MouseTrackingMode | null => {
  if (!arg || arg.trim().toLowerCase() === 'toggle') {
    return current === 'off' ? 'all' : 'off'
  }

  return MOUSE_MODE_ALIASES[arg.trim().toLowerCase()] ?? null
}

const RESET_WORDS = new Set(['reset', 'clear', 'default'])
const CYCLE_WORDS = new Set(['cycle', 'toggle'])

const DETAILS_USAGE =
  'usage: /details [hidden|collapsed|expanded|cycle]  or  /details <section> [hidden|collapsed|expanded|reset]'

const DETAILS_SECTION_USAGE = 'usage: /details <section> [hidden|collapsed|expanded|reset]'

// Shown when /exit or /quit is refused in the hosted dashboard chat. Kept as a
// constant so the test asserts against the same source of truth as production.
export const DASHBOARD_EXIT_DISABLED_MESSAGE =
  'exit is disabled in hosted dashboard chat — use /new to start a fresh session'

export const coreCommands: SlashCommand[] = [
  {
    help: 'list commands + hotkeys',
    name: 'help',
    run: (_arg, ctx) => {
      const sections: PanelSection[] = (ctx.local.catalog?.categories ?? []).map(cat => ({
        rows: cat.pairs,
        title: cat.name
      }))

      if (ctx.local.catalog?.skillCount) {
        sections.push({ text: `${ctx.local.catalog.skillCount} skill commands available — /skills to browse` })
      }

      sections.push(
        {
          rows: [
            ['/details [hidden|collapsed|expanded|cycle]', 'set global agent detail visibility mode'],
            [
              '/details <section> [hidden|collapsed|expanded|reset]',
              'override one section (thinking/tools/subagents/activity)'
            ]
          ],
          title: 'TUI'
        },
        { rows: HOTKEYS, title: 'Hotkeys' }
      )

      ctx.transcript.panel(ctx.ui.theme.brand.helpHeader, sections)
    }
  },

  {
    aliases: ['exit'],
    help: 'exit AstralNexus',
    name: 'quit',
    run: (_arg, ctx) => {
      // In the hosted dashboard chat there is no in-page restart path after
      // the PTY child exits, so quitting bricks the tab until a refresh. The
      // keyboard idle-exit (Ctrl+C / Ctrl+D) and SIGINT handling already refuse
      // to die in this mode (see useInputHandlers + entry.tsx); gate /exit and
      // /quit on the same DASHBOARD_TUI_MODE flag. Unlike the keyboard path
      // (which auto-starts a fresh chat), the explicit quit command refuses and
      // instructs the user to run /new themselves.
      if (DASHBOARD_TUI_MODE) {
        ctx.transcript.sys(DASHBOARD_EXIT_DISABLED_MESSAGE)

        return
      }

      ctx.session.die()
    }
  },

  {
    aliases: ['scroll'],
    help: 'set mouse tracking preset [on|off|toggle|wheel|buttons|all]',
    name: 'mouse',
    run: (arg, ctx) => {
      const current = ctx.ui.mouseTracking
      const next = mouseModeFromArg(arg, current)

      if (next === null) {
        return ctx.transcript.sys('usage: /mouse [on|off|toggle|wheel|buttons|all]')
      }

      patchUiState({ mouseTracking: next })
      ctx.gateway.rpc<ConfigSetResponse>('config.set', { key: 'mouse', value: next }).catch(() => {})

      queueMicrotask(() => ctx.transcript.sys(`mouse tracking ${next}`))
    }
  },

  {
    help: 'force a full UI repaint',
    name: 'redraw',
    run: (_arg, ctx) => {
      forceRedraw(process.stdout)
      ctx.transcript.sys('ui redrawn')
    }
  },

  {
    help: 'set or show current session title',
    name: 'title',
    run: (arg, ctx) => {
      if (!ctx.sid) {
        return ctx.transcript.sys('no active session')
      }

      const title = arg.trim()

      if (!arg) {
        ctx.gateway
          .rpc<SessionTitleResponse>('session.title', { session_id: ctx.sid })
          .then(
            ctx.guarded<SessionTitleResponse>(r => {
              const current = (r?.title ?? '').trim()
              ctx.transcript.sys(current ? `title: ${current}` : 'no title set')
            })
          )
          .catch(ctx.guardedErr)

        return
      }

      if (!title) {
        return ctx.transcript.sys('usage: /title <your session title>')
      }

      ctx.gateway
        .rpc<SessionTitleResponse>('session.title', { session_id: ctx.sid, title })
        .then(
          ctx.guarded<SessionTitleResponse>(r => {
            const next = (r?.title ?? title).trim()
            const suffix = r?.pending ? ' (queued while session initializes)' : ''
            ctx.transcript.sys(`session title set: ${next}${suffix}`)
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'toggle compact transcript',
    name: 'compact',
    run: (arg, ctx) => {
      const next = flagFromArg(arg, ctx.ui.compact)

      if (next === null) {
        return ctx.transcript.sys('usage: /compact [on|off|toggle]')
      }

      patchUiState({ compact: next })
      ctx.gateway.rpc<ConfigSetResponse>('config.set', { key: 'compact', value: next ? 'on' : 'off' }).catch(() => {})

      queueMicrotask(() => ctx.transcript.sys(`compact ${next ? 'on' : 'off'}`))
    }
  },

  {
    aliases: ['detail'],
    help: 'control agent detail visibility (global or per-section)',
    name: 'details',
    run: (arg, ctx) => {
      const { gateway, transcript, ui } = ctx

      if (!arg) {
        gateway
          .rpc<ConfigGetValueResponse>('config.get', { key: 'details_mode' })
          .then(r => {
            if (ctx.stale()) {
              return
            }

            const mode = parseDetailsMode(r?.value) ?? ui.detailsMode
            patchUiState({ detailsMode: mode, detailsModeCommandOverride: false })

            const overrides = SECTION_NAMES.filter(s => ui.sections[s])
              .map(s => `${s}=${ui.sections[s]}`)
              .join(' ')

            transcript.sys(`details: ${mode}${overrides ? `  (${overrides})` : ''}`)
          })
          .catch(() => !ctx.stale() && transcript.sys(`details: ${ui.detailsMode}`))

        return
      }

      const [first, second] = arg.trim().toLowerCase().split(/\s+/)

      if (second && isSectionName(first)) {
        const reset = RESET_WORDS.has(second)
        const mode = reset ? null : parseDetailsMode(second)

        if (!reset && !mode) {
          return transcript.sys(DETAILS_SECTION_USAGE)
        }

        const { [first]: _drop, ...rest } = ui.sections

        patchUiState({ sections: mode ? { ...rest, [first]: mode } : rest })
        gateway
          .rpc<ConfigSetResponse>('config.set', { key: `details_mode.${first}`, value: mode ?? '' })
          .catch(() => {})
        transcript.sys(`details ${first}: ${mode ?? 'reset'}`)

        return
      }

      const next = CYCLE_WORDS.has(first ?? '') ? nextDetailsMode(ui.detailsMode) : parseDetailsMode(first)

      if (!next) {
        return transcript.sys(DETAILS_USAGE)
      }

      const sections = Object.fromEntries(SECTION_NAMES.map(section => [section, next]))

      patchUiState({ detailsMode: next, detailsModeCommandOverride: true, sections })
      gateway.rpc<ConfigSetResponse>('config.set', { key: 'details_mode', value: next }).catch(() => {})
      transcript.sys(`details: ${next}`)
    }
  },

  {
    help: 'copy selection or assistant message',
    name: 'copy',
    run: async (arg, ctx) => {
      const { sys } = ctx.transcript

      if (!arg && ctx.composer.hasSelection) {
        const text = await ctx.composer.selection.copySelection()

        if (text) {
          return sys(`copied ${text.length} characters`)
        } else {
          return sys('clipboard copy failed — try ASTRALNEXUS_TUI_FORCE_OSC52=1 to force the escape sequence')
        }
      }

      if (arg && Number.isNaN(parseInt(arg, 10))) {
        return sys('usage: /copy [number]')
      }

      const all = ctx.local.getHistoryItems().filter(m => m.role === 'assistant')
      const target = all[arg ? Math.min(parseInt(arg, 10), all.length) - 1 : all.length - 1]

      if (!target) {
        return sys('nothing to copy — start a conversation first')
      }

      void writeClipboardText(target.text)
        .then(nativeOk => {
          if (ctx.stale()) {
            return
          }

          if (nativeOk) {
            sys('copied to clipboard')
          } else {
            writeOsc52Clipboard(target.text)
            sys('sent OSC52 copy sequence (terminal support required)')
          }
        })
        .catch(error => {
          if (!ctx.stale()) {
            sys(`copy failed: ${String(error)}`)
          }
        })
    }
  },

  {
    help: 'attach clipboard image',
    name: 'paste',
    run: (arg, ctx) => (arg ? ctx.transcript.sys('usage: /paste') : ctx.composer.paste())
  },

  {
    aliases: ['compose'],
    help: 'compose your next prompt in $EDITOR (same as Ctrl+G)',
    name: 'prompt',
    run: (arg, ctx) => {
      if (arg) {
        // The TUI editor opens with the current composer draft; there is no
        // separate seed arg. Drop any inline text into the composer first so
        // it carries into the editor, matching the CLI's /prompt <text>.
        ctx.composer.setInput(arg)
      }

      void ctx.composer.openEditor().catch((err: unknown) => {
        ctx.transcript.sys(`editor failed: ${String(err)}`)
      })
    }
  },

  {
    help: 'configure IDE terminal keybindings for multiline + undo/redo',
    name: 'terminal-setup',
    run: (arg, ctx) => {
      const target = arg.trim().toLowerCase()

      if (target && !['auto', 'cursor', 'vscode', 'windsurf'].includes(target)) {
        return ctx.transcript.sys('usage: /terminal-setup [auto|vscode|cursor|windsurf]')
      }

      const runner =
        !target || target === 'auto'
          ? configureDetectedTerminalKeybindings()
          : configureTerminalKeybindings(target as 'cursor' | 'vscode' | 'windsurf')

      void runner
        .then(result => {
          if (ctx.stale()) {
            return
          }

          ctx.transcript.sys(result.message)

          if (result.success && result.requiresRestart) {
            ctx.transcript.sys('restart the IDE terminal for the new keybindings to take effect')
          }
        })
        .catch(error => {
          if (!ctx.stale()) {
            ctx.transcript.sys(`terminal setup failed: ${String(error)}`)
          }
        })
    }
  },

  {
    help: 'view gateway logs',
    name: 'logs',
    run: (arg, ctx) => {
      const text = ctx.gateway.gw.getLogTail(Math.min(80, Math.max(1, parseInt(arg, 10) || 20)))

      text ? ctx.transcript.page(text, 'Logs') : ctx.transcript.sys('no gateway logs')
    }
  },

  {
    aliases: ['sb'],
    help: 'status bar position (on|off|top|bottom)',
    name: 'statusbar',
    run: (arg, ctx) => {
      const mode = arg.trim().toLowerCase()
      const toggle: StatusBarMode = ctx.ui.statusBar === 'off' ? 'top' : 'off'

      const next: null | StatusBarMode =
        !mode || mode === 'toggle'
          ? toggle
          : mode === 'on' || mode === 'top'
            ? 'top'
            : mode === 'off' || mode === 'bottom'
              ? mode
              : null

      if (!next) {
        return ctx.transcript.sys('usage: /statusbar [on|off|top|bottom|toggle]')
      }

      patchUiState({ statusBar: next })
      ctx.gateway.rpc<ConfigSetResponse>('config.set', { key: 'statusbar', value: next }).catch(() => {})

      queueMicrotask(() => ctx.transcript.sys(`status bar ${next}`))
    }
  }
]
