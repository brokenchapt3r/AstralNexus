import { coreCommands } from './commands/core.js'
import type { SlashCommand } from './types.js'

// Only terminal-presentation behavior is implemented in the Ink process.
// Runtime, task, model, tool and session-lifecycle commands are supplied by
// the Python command catalog and dispatched through the Gateway.
export const FRONTEND_OWNED_COMMANDS = new Set([
  'agents',
  'plan', 'evidence', 'delivery', 'changes', 'restore', 'accept', 'reject', 'checkpoint', 'checkpoints',
  'model', 'submodel',
  'help',
  'quit',
  'mouse',
  'redraw',
  'compact',
  'details',
  'copy',
  'paste',
  'prompt',
  'terminal-setup',
  'logs',
  'statusbar',
  'title'
])

export const SLASH_COMMANDS: SlashCommand[] = coreCommands.filter(command =>
  FRONTEND_OWNED_COMMANDS.has(command.name)
)

const byName = new Map<string, SlashCommand>(
  SLASH_COMMANDS.flatMap(cmd => [cmd.name, ...(cmd.aliases ?? [])].map(name => [name, cmd] as const))
)

export const findSlashCommand = (name: string) => byName.get(name.toLowerCase())
