import type { Snapshot } from '../types'

// Only the installer's known layout may select a Claude directory.
export function claudeDirectory(root: string): string | null {
  const normalized = root.replace(/\\/g, '/').replace(/\/+$/, '')
  const match = /^(.*)\/agentfleet\/mods\/fleet-status$/.exec(normalized)
  const directory = match?.[1]
  return directory && directory !== '/' && !/^[A-Za-z]:$/.test(directory) ? directory : null
}

export function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null
}
function text(value: unknown, pattern: RegExp): string | null {
  if (typeof value !== 'string') return null
  const trimmed = value.trim()
  // Reject unsafe input before sanitizing so it cannot become a valid identifier.
  if (/[\u0000-\u001f\u007f-\u009f\p{Cf}\u2028\u2029]/u.test(trimmed)) return null
  const safe = trimmed.replace(/[\u0000-\u001f\u007f-\u009f\p{Cf}\u2028\u2029]/gu, '')
  return pattern.test(safe) ? safe : null
}
const versionPattern = /^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]{1,32})?$/

export function snapshotFromSources(claudeDir: string | null, sources: (string | null)[]): Snapshot {
  function json(source: string | null | undefined): Record<string, unknown> | null {
    try { return record(JSON.parse(source ?? 'null')) } catch { return null }
  }
  const manifest = json(sources[0])
  const profile = sources[1]
  const auto = json(sources[2])
  const proposal = json(sources[3])
  const result = record(auto?.lastResult)
  return {
    claudeDir,
    version: text(manifest?.version, versionPattern),
    profile: text(profile, /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/),
    enabled: typeof auto?.enabled === 'boolean' ? auto.enabled : null,
    lastCheck: text(auto?.lastCheck, /^[0-9T:.Z+-]{1,40}$/),
    latestSeen: text(auto?.latestSeen, versionPattern),
    lastResult: typeof result?.ok === 'boolean'
      ? { ok: result.ok, version: text(result.version, versionPattern) } : null,
    reported: typeof auto?.reported === 'boolean' ? auto.reported : null,
    decision: proposal?.decision === 'approved' || proposal?.decision === 'pending' ? proposal.decision : null,
    missingLanes: Object.keys(record(proposal?.missing_lane_models) ?? {}).length,
  }
}

export function notices(snapshot: Snapshot, versionAtStart: string | null): string[] {
  const lines: string[] = []
  const result = snapshot.lastResult
  if (snapshot.version !== null && versionAtStart !== null && snapshot.version !== versionAtStart) {
    lines.push(`AgentFleet ${snapshot.version} is installed. Restart Claude Code to use it.`)
  }
  if (result?.ok === false && !snapshot.reported) {
    lines.push(`AgentFleet update to ${result.version ?? 'unknown'} failed. See ${(snapshot.claudeDir ?? '').replace(/[\u0000-\u001f\u007f-\u009f\p{Cf}\u2028\u2029]/gu, '').replace(/\/$/, '')}/agentfleet/auto-update.log`)
  }
  if (snapshot.missingLanes > 0) {
    lines.push(`${snapshot.missingLanes} fleet ${snapshot.missingLanes === 1 ? 'lane has' : 'lanes have'} no available model. Run /fleet-setup.`)
  }
  if (snapshot.decision === 'pending') {
    lines.push('Model changes are waiting for review. Run /fleet-setup.')
  }
  return lines
}

export function summary(snapshot: Snapshot, versionAtStart: string | null): string {
  const result = snapshot.lastResult
  const action = notices(snapshot, versionAtStart)
  return [
    `AgentFleet version: ${snapshot.version ?? 'unknown'}`,
    `Profile: ${snapshot.profile ?? 'default'}`,
    `Auto-update: ${snapshot.enabled === null ? 'unknown' : snapshot.enabled ? 'enabled' : 'disabled'}`,
    `Last check: ${snapshot.lastCheck ?? 'unknown'}; latest seen: ${snapshot.latestSeen ?? 'unknown'}`,
    `Last result: ${result ? `${result.version ?? 'unknown'} (${result.ok ? 'succeeded' : 'failed'})` : 'none'}`,
    `Model proposals: ${snapshot.decision ?? 'unknown'}; missing lanes: ${snapshot.missingLanes}`,
    ...(action.length ? action : ['No action needed.']),
  ].join('\n')
}
