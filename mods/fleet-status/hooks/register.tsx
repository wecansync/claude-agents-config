import { atom, read, update } from 'claude-code'
import type { CoreEngineInterface, Register } from 'claude-code'
import type { FleetState, Snapshot } from '../types'
import { claudeDirectory, notices, snapshotFromSources, summary } from './status'

export async function readSnapshot($: CoreEngineInterface): Promise<Snapshot> {
  const claudeDir = claudeDirectory($.plugin.root)
  if (claudeDir === null) return snapshotFromSources(null, [])
  const base = claudeDir.replace(/\/$/, '')
  async function readSource(path: string): Promise<string | null> {
    try { return await $.fs.read(`${base}/${path}`) } catch { return null }
  }
  const sources = await Promise.all([
    readSource('.claude-agents-config-manifest.json'),
    readSource('agentfleet/active'),
    readSource('agentfleet/auto-update.json'),
    readSource('fleet-model-proposal.json'),
  ])
  return snapshotFromSources(claudeDir, sources)
}

function refreshed(current: FleetState, snapshot: Snapshot): FleetState {
  const previousKey = JSON.stringify(current.snapshot ? notices(current.snapshot, current.versionAtStart) : [])
  const newKey = JSON.stringify(notices(snapshot, current.versionAtStart))
  return { ...current, snapshot, hiddenNoticeKey: previousKey === newKey ? current.hiddenNoticeKey : null }
}

const status = atom({ plugin: 'fleet-status', key: 'status' } as const, {
  snapshot: null, versionAtStart: null, hiddenNoticeKey: null,
} as FleetState)

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const snapshot = await readSnapshot($)
    await update($, status, () => ({ snapshot, versionAtStart: snapshot.version, hiddenNoticeKey: null }))
    await $.command.register({
      name: 'fleet-status',
      description: 'Show AgentFleet status: version, profile, updates, and model proposals',
    })
    $.clock.every(60_000, async () => {
      const snapshot = await readSnapshot($)
      await update($, status, current => refreshed(current, snapshot))
    })
    return next(e)
  })

  on('command.run', { command: 'fleet-status' }, async $ => {
    const snapshot = await readSnapshot($)
    const current = await read($, status)
    await update($, status, state => refreshed(state, snapshot))
    return { text: summary(snapshot, current.versionAtStart) }
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) return next(e)
    const current = await read($, status)
    const lines = current.snapshot ? notices(current.snapshot, current.versionAtStart) : []
    const key = JSON.stringify(lines)
    if (!lines.length || current.hiddenNoticeKey === key) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    return (
      <Box flexDirection="column" width={e.props.bodyColumns}>
        <Box>
          <Text dimColor>AgentFleet </Text>
          <Button key="hide" label="Hide" onPress={() => update($, status, state => ({ ...state, hiddenNoticeKey: key }))} />
        </Box>
        {lines.map((line, index) => <Text key={`notice-${index}`} wrap="wrap">{line}</Text>)}
      </Box>
    )
  })
}
