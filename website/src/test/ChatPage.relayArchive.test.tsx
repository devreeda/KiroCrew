/**
 * A chat that ran on a crew (`executor === 'remote'`) is a read-only archive.
 *
 * The composer is disabled and one line above it says where the conversation
 * continues: the crew's own session. A plain local chat keeps a live composer
 * and shows no such line. The memory-mode recreate no longer carries the
 * archive's `instance_id`, because that carry was the last UI path that minted
 * a new crew-bound slot.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { ThemeProvider } from '../hooks/useTheme'
import enManual from '../i18n/locales/en.manual.json'

vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (i: number, d: unknown) => React.ReactNode }) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))
const { listInstancesMock } = vi.hoisted(() => ({ listInstancesMock: vi.fn() }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: false, has_more: false, total: 1 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    listInstances: listInstancesMock,
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'
import { ApiError } from '../api/apiError'
import { closeCrewWindow, currentCrewWindow } from '../pages/chat/crew-window/crewWindowStore'

const ARCHIVE = enManual.pages.chat.relayArchive

function makeStore(slot: Record<string, unknown>) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: [{ key: 'slot-a', messages: 1, running: false, stop_state: 'idle', mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, ...slot }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as ReturnType<typeof dashboardReducer>,
      chat: {
        activeSlot: 'slot-a', messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as ReturnType<typeof chatReducer>,
      notifications: { items: [] } as unknown as ReturnType<typeof notificationsReducer>,
    },
  })
}

async function renderPage(slot: Record<string, unknown>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={makeStore(slot)}>
          <ThemeProvider>
            <MemoryRouter><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
}

beforeEach(() => {
  listInstancesMock.mockReset().mockResolvedValue({ active: true, instances: [{ id: 'inst-1', name: 'astro' }] })
  sessionStorage.clear()
  localStorage.clear()
})

describe('ChatPage — a chat that ran on a crew is read-only', { timeout: 15_000 }, () => {
  it('disables the composer and says where to keep going', async () => {
    await renderPage({ executor: 'remote', instance_id: 'inst-1', remote_slot: 'peer-slot' })

    const notice = await screen.findByTestId('relay-archive-notice')
    await waitFor(() => expect(notice).toHaveTextContent(ARCHIVE.notice.replace('{{name}}', 'astro')))
    expect(notice).toHaveAttribute('role', 'status')
    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(input.className).toMatch(/pointer-events-none/)
    expect(input.placeholder).toBe(ARCHIVE.placeholder)
  })

  it('opens the crew\'s own session from the notice', async () => {
    closeCrewWindow()
    await renderPage({ executor: 'remote', instance_id: 'inst-1', row_identity: 'inst-1:peer-slot' })

    const open = await screen.findByTestId('relay-archive-open')
    await waitFor(() => expect(open).toHaveTextContent(ARCHIVE.open.replace('{{name}}', 'astro')))
    act(() => { open.click() })
    expect(currentCrewWindow()).toEqual({ instanceId: 'inst-1', key: 'peer-slot' })
    closeCrewWindow()
  })

  it('offers no open button when the row names no peer session', async () => {
    await renderPage({ executor: 'remote', instance_id: 'inst-1', row_identity: 'chat-a' })
    await screen.findByTestId('relay-archive-notice')
    expect(screen.queryByTestId('relay-archive-open')).toBeNull()
  })

  it('shows a failed crew lookup, keeping the instance id as the name', async () => {
    listInstancesMock.mockRejectedValue(new ApiError(500, 'gateway exploded'))
    await renderPage({ executor: 'remote', instance_id: 'inst-1', remote_slot: 'peer-slot' })

    const err = await screen.findByTestId('relay-archive-lookup-error')
    expect(err).toHaveTextContent('gateway exploded')
    expect(screen.getByTestId('relay-archive-notice')).toHaveTextContent(ARCHIVE.notice.replace('{{name}}', 'inst-1'))
  })

  it('says nothing more when crews are turned off on this install', async () => {
    listInstancesMock.mockRejectedValue(new ApiError(403, 'instances disabled'))
    await renderPage({ executor: 'remote', instance_id: 'inst-1', remote_slot: 'peer-slot' })

    await waitFor(() => expect(listInstancesMock).toHaveBeenCalled())
    await screen.findByTestId('relay-archive-notice')
    expect(screen.queryByTestId('relay-archive-lookup-error')).toBeNull()
  })

  it('keeps a plain local chat live, with no archive line', async () => {
    await renderPage({})

    expect(screen.queryByTestId('relay-archive-notice')).toBeNull()
    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(input.className).not.toMatch(/pointer-events-none/)
    expect(input.placeholder).not.toBe(ARCHIVE.placeholder)
  })
})

describe('ChatPage — send() refuses an archive on every path', () => {
  // Source contract: follow-up chips and quick send reach send() without the
  // composer, so the refusal must sit in send() before the draft is cleared.
  const here = dirname(fileURLToPath(import.meta.url))
  const src = readFileSync(resolve(here, '../pages/ChatPage.tsx'), 'utf8')
  const start = src.indexOf('const send = useCallback(async')
  const head = src.slice(start, src.indexOf('const raw = (isolated', start))

  it('returns early for a remote-bound target slot', () => {
    expect(start).toBeGreaterThan(-1)
    expect(head).toMatch(/if \(slotIsRemoteBound\(boundStore\.getState\(\)\.dashboard\.slots\.find\(s => s\.key === \(targetSlot \?\? activeSlotRef\.current\)\)\)\) return false/)
  })
})

describe('ChatPage — the memory-mode recreate mints no crew-bound slot', () => {
  // Source contract, as in ChatPage.remoteBoundGates.test.tsx: the recreate runs
  // from the welcome screen's memory chip, which a jsdom render does not reach.
  const here = dirname(fileURLToPath(import.meta.url))
  const src = readFileSync(resolve(here, '../pages/ChatPage.tsx'), 'utf8')
  const start = src.indexOf('const switchMemoryMode = async')
  const body = src.slice(start, src.indexOf('dispatch(createSlot(opts))', start))

  it('builds the recreate options without the old slot\'s instance id', () => {
    expect(start).toBeGreaterThan(-1)
    expect(body).toContain('memory_mode: newMode')
    expect(body).not.toMatch(/instanceId|instance_id/)
  })
})
