import { execFileSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { findSlashCommand, FRONTEND_OWNED_COMMANDS, SLASH_COMMANDS } from '../app/slash/registry.js'
import { getOverlayState, resetOverlayState } from '../app/overlayStore.js'

type CommandRoute = 'fallback' | 'local' | 'native'

interface CommandRegistryLoad {
  error?: string
  names: string[]
}

const NATIVE_MUTATING_COMMANDS = new Set<string>()

const loadCommandRegistryNames = (): CommandRegistryLoad => {
  const here = dirname(fileURLToPath(import.meta.url))

  try {
    const names = JSON.parse(
      execFileSync(
        process.env.PYTHON ?? 'python3',
        [
          '-c',
          'import json; from AstralNexus.cli.commands import COMMAND_REGISTRY; print(json.dumps([c.name for c in COMMAND_REGISTRY]))'
        ],
        { cwd: resolve(here, '../../..'), encoding: 'utf8' }
      )
    ) as string[]

    return { names: [...new Set(names)] }
  } catch (error) {
    return {
      error: error instanceof Error ? error.message : String(error),
      names: []
    }
  }
}

const commandRegistry = loadCommandRegistryNames()
const registryIt = commandRegistry.error ? it.skip : it
const skipReason = commandRegistry.error ? commandRegistry.error.split('\n')[0] : ''

const LOCAL_COMMAND_NAMES = new Set(
  SLASH_COMMANDS.flatMap(command => [command.name, ...(command.aliases ?? [])].map(name => name.toLowerCase()))
)

const classifyRoute = (name: string): CommandRoute => {
  const normalized = name.toLowerCase()

  if (NATIVE_MUTATING_COMMANDS.has(normalized)) {
    return 'native'
  }

  if (LOCAL_COMMAND_NAMES.has(normalized)) {
    return 'local'
  }

  return 'fallback'
}

describe('slash parity matrix', () => {
  it('opens the live agents panel through the advertised local command', () => {
    resetOverlayState()
    const command = findSlashCommand('agents')
    expect(command).toBeDefined()
    command!.run('', {} as any, 'agents')
    expect(getOverlayState().agents).toBe(true)
  })
  it('opens real inspection views and exposes explicit restore', () => {
    for (const name of ['plan', 'evidence', 'delivery', 'changes'] as const) {
      findSlashCommand(name)!.run('', {} as any, name)
      expect(getOverlayState().inspection).toBe(name)
    }
    expect(findSlashCommand('restore')).toBeDefined()
  })
  if (commandRegistry.error) {
    it.skip(`Python command registry unavailable: ${skipReason}`, () => {})
  }

  registryIt('loads the AstralNexus command registry without duplicate commands', () => {
    expect(new Set(commandRegistry.names).size).toBe(commandRegistry.names.length)
    for (const required of ['run', 'resume', 'status', 'interrupt', 'dashboard', 'quit']) {
      expect(commandRegistry.names, `missing command in registry: ${required}`).toContain(required)
    }
  })

  registryIt('classifies each backend command as local/native/gateway fallback', () => {
    const routes = Object.fromEntries(commandRegistry.names.map(name => [name, classifyRoute(name)]))

    for (const name of commandRegistry.names) {
      expect(['fallback', 'local', 'native']).toContain(routes[name])
    }
  })

  it('does not expose backend-owned AstralNexus command implementations locally', () => {
    for (const name of ['tools', 'skills', 'status', 'journey', 'subscription', 'topup']) {
      expect(findSlashCommand(name), `${name} must use the Gateway command catalog`).toBeUndefined()
    }
  })

  it('registers exactly the declared presentation command boundary', () => {
    expect(new Set(SLASH_COMMANDS.map(command => command.name))).toEqual(FRONTEND_OWNED_COMMANDS)
  })
})
