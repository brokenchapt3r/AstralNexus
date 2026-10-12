import type { SessionInfo } from '../types.js'
import { getUiState } from './uiStore.js'

/** Missing legacy advertisements are distinct from an explicit unsupported capability. */
export function supportsCapability(key: keyof NonNullable<SessionInfo['capabilities']>, info = getUiState().info): boolean {
  return info?.capabilities?.[key] !== false
}
