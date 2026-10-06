/**
 * Screenshot harness for a chat that ran on a crew: a read-only archive.
 * Runs the built SPA (website/dist) with every /api/** answered from fixtures.
 * Usage: node scripts/capture-relay-archive.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/relay-archive'
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

const ASTRO = {
  id: 'astro', name: 'astro', ssh_host: 'astro', remote_port: 5480, local_port: 7778,
  ttl: '20h', remote_bin: '', connection_method: 'ssh', ssm_target: '', ssm_run_as: '',
  aws_profile: '', aws_region: '', was_connected: true,
  status: { instance_id: 'astro', state: 'connected', local_port: 7778, remote_port: 5480 },
}
const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }
const now = Date.now()
const SLOTS = [
  { key: 'chat-archive', title: 'Rotate the deploy keys', messages: 2, running: false, agent: 'kirocrew',
    created: '2026-09-13T20:00:00Z', last_ts: new Date(now - 60_000).toISOString(), folder_id: '',
    executor: 'remote', instance_id: 'astro', row_identity: 'astro:chat-9' },
  { key: 'chat-local', title: 'Release notes draft', messages: 2, running: false, agent: 'kirocrew',
    created: '2026-09-13T20:00:00Z', last_ts: new Date(now - 120_000).toISOString(), folder_id: '' },
]

async function main() {
  const { srv, base } = await serveDist()
  const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
  const browser = await chromium.launch({ env: browserEnv })
  const context = await browser.newContext({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} } })
  await stubDashboardApi(page, {
    folders: [], slots: SLOTS,
    extra: async (path, route) => {
      if (path === '/api/instances') { await json(route, { active: true, instances: [ASTRO], warm_set_cap: 5, sso: SSO }); return true }
      if (path === '/api/instances/astro/capabilities') { await json(route, { instance_id: 'astro', version: '0.9.0', local_version: '0.9.0', version_match: true, agents: [], models: [], workspaces: [] }); return true }
      if (path.startsWith('/api/instances/')) { await json(route, { ok: true }); return true }
      if (path === '/api/chat/slots/chat-archive') {
        await json(route, { messages: [
          { role: 'user', content: 'List the deploy user\'s access keys, then rotate the old one.', ts: '2026-09-13T20:00:00Z', meta: { mid: 'a-1' } },
          { role: 'assistant', content: 'Done. The old key is deactivated and the new one is in the vault.', ts: '2026-09-13T20:01:00Z', meta: { mid: 'a-2' } },
        ], has_more: false, total: 2 })
        return true
      }
      return false
    },
  })
  logPageProblems(page)

  await page.goto(`${base}/chat?sid=chat-archive`, { waitUntil: 'domcontentloaded' })
  const notice = page.locator('[data-testid="relay-archive-notice"]')
  await notice.waitFor({ state: 'visible', timeout: 20_000 })
  await page.waitForFunction(() => document.querySelector('[data-testid="relay-archive-notice"]')?.textContent?.includes('astro'))
  const text = (await notice.textContent()) ?? ''
  if (!text.includes('This chat ran on astro')) throw new Error(`notice text: ${text}`)
  const ph = await page.getByLabel('Message input').getAttribute('placeholder')
  if (ph !== 'Read-only') throw new Error(`placeholder: ${ph}`)
  if (!(await page.locator('[data-testid="relay-archive-open"]').isVisible())) throw new Error('no open button')
  const chip = page.locator('[data-slot-key="chat-archive"] [data-testid="remote-crew-chip"]')
  if (((await chip.textContent()) ?? '').trim() !== 'Ran on astro') throw new Error('chip label')
  await page.mouse.move(900, 60)
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${PREFIX}-1-archive.png` })
  await chip.hover()
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/${PREFIX}-2-sidebar-chip.png`, clip: { x: 0, y: 0, width: 420, height: 420 } })
  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/${PREFIX}-1-archive.png, ${PREFIX}-2-sidebar-chip.png`)
}

main().catch(e => { console.error(e); process.exit(1) })
