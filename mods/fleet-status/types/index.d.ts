export type Snapshot = {
  claudeDir: string | null
  version: string | null
  profile: string | null
  enabled: boolean | null
  lastCheck: string | null
  latestSeen: string | null
  lastResult: { version: string | null; ok: boolean } | null
  reported: boolean | null
  decision: 'approved' | 'pending' | null
  missingLanes: number
}

export type FleetState = {
  snapshot: Snapshot | null
  versionAtStart: string | null
  hiddenNoticeKey: string | null
}

declare module 'claude-code' {
  interface PluginState {
    'fleet-status': { status: FleetState }
  }
}
