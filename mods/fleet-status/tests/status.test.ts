import { test, expect, mock } from 'claude-code/testing'
import type { CoreEngineInterface, On, RenderPropsOf } from 'claude-code'
import { claudeDirectory, snapshotFromSources, notices, summary } from '../hooks/status'
import { readSnapshot } from '../hooks/register'

// Test the installed layout rather than this repository's source layout.


const props: RenderPropsOf['AbovePrompt'] = {
  hasSurvey: false, isWorking: false, maxRows: 12, bodyColumns: 70,
  scroll: { offset: 0, bodyRows: 12 }, view: {},
}
const start = { cwd: '/test', surface: 'terminal' as const, isInteractive: true }

// The kit supports fs interception through ordinary test hooks, not mock.fs.
// Verify every path: a forbidden read fails instead of silently using real disk.
function sources(on: On, files: Record<string, string>, root = '/test/claude/agentfleet/mods/fleet-status') {
  // Plugin identity is immutable in the kit. Supply installed-layout snapshots
  // through state writes while exercising the real read function separately.
  const readFixture = async () => readSnapshot({
    plugin: { name: 'fleet-status', root },
    fs: { read: async (path: string) => {
      const name = allowed.find(name => path.endsWith(`/${name}`))
      expect(name).toBeDefined()
      if (!name || files[name] === undefined) throw new Error('Missing fixture')
      return files[name]!
    } },
  } as unknown as CoreEngineInterface)
  let started = false
  let initialVersion: string | null = null
  let previousKey = ''
  let hiddenKey: string | null = null
  on('state.set', { plugin: 'fleet-status', key: 'status' }, async (_$, e, next) => {
    const snapshot = await readFixture()
    if (!started) { initialVersion = snapshot.version; started = true }
    const key = JSON.stringify(notices(snapshot, initialVersion))
    hiddenKey = key !== previousKey ? null : e.value.hiddenNoticeKey ?? hiddenKey
    previousKey = key
    return next({ ...e, value: { ...e.value, snapshot, versionAtStart: initialVersion, hiddenNoticeKey: hiddenKey } })
  })
  const allowed = [
    '.claude-agents-config-manifest.json', 'agentfleet/active',
    'agentfleet/auto-update.json', 'fleet-model-proposal.json',
  ]
  on('fs.read', (_$, e) => {
    const normalized = e.path.replace(/\\/g, '/')
    const name = allowed.find(name => normalized.endsWith(`/${name}`))
    expect(name).toBeDefined()
    if (!name || files[name] === undefined) return { deny: 'Missing fixture' }
    return { value: files[name]! }
  })
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('ui.render', { component: 'AbovePrompt' }, () => ({ type: 'engine', ref: 0 }))
  return mock.clock(on)
}
function healthy() {
  return {
    '.claude-agents-config-manifest.json': '{"version":"2.1.0"}',
    'fleet-model-proposal.json': '{"decision":"approved","missing_lane_models":{}}',
    'agentfleet/auto-update.json': '{"enabled":true}',
  }
}

test('healthy sources show no band and a fresh command summary', async ($, on) => {
  const files = healthy()
  sources(on, files)
  await $.session.start(start)
  const ui = await $.ui.mount({ plugin: 'fleet-status', surface: 'terminal', component: 'AbovePrompt', props })
  expect(await ui.drawn()).toEqual({ type: 'engine', ref: 0 })
  const reply = { text: summary(snapshotFromSources('/test/claude', [files['.claude-agents-config-manifest.json'], null, files['agentfleet/auto-update.json'], files['fleet-model-proposal.json']]), '2.1.0') }
  expect(reply.text).toContain('2.1.0')
  expect(reply.text).toContain('No action needed.')
  files['.claude-agents-config-manifest.json'] = '{"version":"2.2.0"}'
  expect(summary(snapshotFromSources('/test/claude', [files['.claude-agents-config-manifest.json'], null, files['agentfleet/auto-update.json'], files['fleet-model-proposal.json']]), '2.1.0')).toContain('2.2.0')
})

for (const surface of ['terminal', 'desktop'] as const) {
  test(`pending review and Hide on ${surface}; changed notices reappear`, async ($, on) => {
    const files = healthy()
    files['fleet-model-proposal.json'] = '{"decision":"pending","missing_lane_models":{}}'
    const clock = sources(on, files)
    await $.session.start(start)
    const ui = await $.ui.mount({ plugin: 'fleet-status', surface, component: 'AbovePrompt', props })
    expect((await ui.find({ text: 'Model changes are waiting for review.' }))?.text).toContain('/fleet-setup')
    await ui.press({ key: 'hide' })
    expect(await ui.drawn()).toEqual({ type: 'engine', ref: 0 })
    await clock.advance(60_000)
    expect(await ui.drawn()).toEqual({ type: 'engine', ref: 0 })
    files['fleet-model-proposal.json'] = '{"decision":"pending","missing_lane_models":{"ui":{}}}'
    await clock.advance(60_000)
    expect((await ui.find({ text: '1 fleet lane has' }))?.text).toContain('no available model')
    files['fleet-model-proposal.json'] = '{"decision":"pending","missing_lane_models":{}}'
    await clock.advance(60_000)
    expect((await ui.find({ text: 'Model changes are waiting for review.' }))?.text).toContain('/fleet-setup')
    const survey = await $.ui.mount({ plugin: 'fleet-status', surface, component: 'AbovePrompt', props: { ...props, hasSurvey: true } })
    expect(await survey.drawn()).toEqual({ type: 'engine', ref: 0 })
  })
}

test('refresh compares the installed update to the session start version', async ($, on) => {
  const files = healthy()
  files['agentfleet/auto-update.json'] = '{"lastResult":{"version":"2.0.6","ok":true}}'
  const clock = sources(on, files)
  await $.session.start(start)
  const ui = await $.ui.mount({ plugin: 'fleet-status', surface: 'terminal', component: 'AbovePrompt', props })
  expect(await ui.drawn()).toEqual({ type: 'engine', ref: 0 })
  files['.claude-agents-config-manifest.json'] = '{"version":"2.2.0"}'
  // A manual update changes only the manifest; lastResult remains stale.
  await clock.advance(60_000)
  expect((await ui.find({ text: 'AgentFleet 2.2.0 is installed.' }))?.text).toContain('Restart Claude Code')
})

test('manifest missing at start never triggers a restart notice', async ($, on) => {
  const files: Record<string, string> = healthy()
  delete files['.claude-agents-config-manifest.json']
  files['agentfleet/auto-update.json'] = '{"lastResult":{"version":"2.0.6","ok":true}}'
  const clock = sources(on, files)
  await $.session.start(start)
  const ui = await $.ui.mount({ plugin: 'fleet-status', surface: 'terminal', component: 'AbovePrompt', props })
  expect(await ui.drawn()).toEqual({ type: 'engine', ref: 0 })
  files['.claude-agents-config-manifest.json'] = '{"version":"2.2.0"}'
  await clock.advance(60_000)
  expect(await ui.drawn()).toEqual({ type: 'engine', ref: 0 })
  const reply = await $.command.run({ command: 'fleet-status', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } })
  expect(reply.text).toContain('No action needed.')
})

for (const bad of [undefined, '{bad json', '[]', '{"version":7,"lastResult":{"ok":"false"},"decision":true,"missing_lane_models":[]}']) {
  test(`absent or malformed sources are safe: ${bad ?? 'missing'}`, async ($, on) => {
    const files: Record<string, string> = {}
    if (bad !== undefined) {
      for (const name of ['.claude-agents-config-manifest.json', 'agentfleet/auto-update.json', 'fleet-model-proposal.json']) files[name] = bad
    }
    sources(on, files)
    await $.session.start(start)
    const ui = await $.ui.mount({ plugin: 'fleet-status', surface: 'terminal', component: 'AbovePrompt', props })
    expect(await ui.drawn()).toEqual({ type: 'engine', ref: 0 })
    expect((await $.command.run({ command: 'fleet-status', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } })).text).toContain('No action needed.')
  })
}

test('failed update, missing lanes and pending review retain notice order', async ($, on) => {
  const files = healthy()
  files['agentfleet/auto-update.json'] = '{"lastResult":{"version":"2.2.0","ok":false},"reported":false}'
  files['fleet-model-proposal.json'] = '{"decision":"pending","missing_lane_models":{"ui":{},"review":{}}}'
  const clock = sources(on, files)
  await $.session.start(start)
  const ui = await $.ui.mount({ plugin: 'fleet-status', surface: 'desktop', component: 'AbovePrompt', props })
  const lines = await ui.findAll({ type: 'Text' })
  expect(lines[1]?.text).toContain('update to 2.2.0 failed. See ')
  expect(lines[1]?.text).toContain('/agentfleet/auto-update.log')
  expect(lines[2]?.text).toContain('2 fleet lanes have')
  expect(lines[3]?.text).toContain('waiting for review')
  files['agentfleet/auto-update.json'] = '{"lastResult":{"version":"2.2.0","ok":false},"reported":true}'
  await clock.advance(60_000)
  expect(await ui.find({ text: 'failed. See' })).toBeUndefined()
})

test('nonmatching root attempts no reads and shows no band', async ($, on) => {
  let reads = 0
  const snapshot = await readSnapshot({
    plugin: { name: 'fleet-status', root: '/unrelated/mods/fleet-status' },
    fs: { read: async () => { reads++; throw new Error('Unexpected read') } },
  } as unknown as CoreEngineInterface)
  expect(reads).toBe(0)
  expect(snapshot).toEqual(snapshotFromSources(null, []))
  sources(on, healthy(), '/unrelated/mods/fleet-status')
  await $.session.start(start)
  const ui = await $.ui.mount({ plugin: 'fleet-status', surface: 'terminal', component: 'AbovePrompt', props })
  expect(await ui.drawn()).toEqual({ type: 'engine', ref: 0 })
})

for (const profile of ['normal\nsecret', 'x'.repeat(65), 'safe‮hidden']) {
  test(`unsafe profile defaults: ${JSON.stringify(profile)}`, async () => {
    const snapshot = await readSnapshot({
      plugin: { name: 'fleet-status', root: '/test/claude/agentfleet/mods/fleet-status' },
      fs: { read: async (path: string) => path.endsWith('/active') ? profile : '{}' },
    } as unknown as CoreEngineInterface)
    expect(snapshot.profile).toBeNull()
    expect(summary(snapshot, null)).toContain('Profile: default')
    expect(summary(snapshot, null)).not.toContain(profile)
  })
}

test('malformed versions and timestamps are absent and never request restart', async () => {
  const snapshot = snapshotFromSources('/test/claude', [
    '{"version":"2.2.0 injected"}', null,
    '{"latestSeen":"bad","lastCheck":"x","lastResult":{"version":"bad","ok":true}}', '{}',
  ])
  expect(snapshot.version).toBeNull()
  expect(snapshot.latestSeen).toBeNull()
  expect(snapshot.lastCheck).toBeNull()
  expect(snapshot.lastResult?.version).toBeNull()
  expect(summary(snapshot, '2.1.0')).toContain('AgentFleet version: unknown')
  expect(notices(snapshot, '2.1.0')).toEqual([])
})

test('installation paths handle custom directories and both separators', () => {
  expect(claudeDirectory('/custom/claude/agentfleet/mods/fleet-status')).toBe('/custom/claude')
  expect(claudeDirectory('C:\\custom\\claude\\agentfleet\\mods\\fleet-status\\')).toBe('C:/custom/claude')
  expect(claudeDirectory('/agentfleet/mods/fleet-status')).toBeNull()
  expect(claudeDirectory('C:\\agentfleet\\mods\\fleet-status')).toBeNull()
  expect(claudeDirectory('/custom/claude/mods/fleet-status')).toBeNull()
})
