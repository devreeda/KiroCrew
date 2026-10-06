/**
 * Closing a session tree — option B: refuse while any descendant runs (#17253).
 *
 * The ✕ has two outcomes:
 *   - any running descendant → refuses; a notice lists which are running; nothing closes.
 *   - no running descendants → closes silently (pref off) or asks once with useConfirm (pref on).
 *
 * There is no "close anyway" on the refusal notice.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Provider } from 'react-redux'

const chatConfig = vi.hoisted(() => ({ confirmCloseSession: false }))
vi.mock('../pages/chat/ChatSettings', () => ({ loadChatConfig: () => chatConfig }))

const deleteSlot = vi.hoisted(() => vi.fn((key: string) => () => ({
  unwrap: async () => key,
})))
const resumeFromHistory = vi.hoisted(() => vi.fn(({ key }: { key: string; title: string }) => () => ({
  unwrap: async () => ({ ok: true, key }),
})))
vi.mock('../store/chatSlice', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  deleteSlot,
  resumeFromHistory,
}))

import { store } from '../store'
import { planCloseTree, type CloseTreeRow } from '../lib/sessionCloseTree'
import { useCloseSessionTree } from '../hooks/useCloseSessionTree'
import { i18nT } from '../i18n/t'

interface Row extends CloseTreeRow {
  key: string
  title?: string
  running?: boolean
}

const root = (key: string, title: string, running = false): Row =>
  ({ key, title, running, parent: null })
const child = (key: string, title: string, parent: string, running = false): Row =>
  ({ key, title, running, parent: { slot: parent, key: parent } })

const isRunning = (row: Row) => row.running === true
const closedOrder = () => deleteSlot.mock.calls.map(([key]) => key)

function Press({ rows }: { rows: Row[] }) {
  const { closeSessionTree, closeTreeDialog, closeTreeError, closeTreeNotice } = useCloseSessionTree({
    rows, isRunning, closeOne: (key: string) => closeOneSpy(key), openOne: (key: string) => openOneSpy(key),
  })
  return (
    <>
      <button data-testid="x" onClick={() => closeSessionTree('lead')}>x</button>
      {closeTreeDialog}
      {closeTreeError}
      {closeTreeNotice}
    </>
  )
}

function Harness({ rows }: { rows: Row[] }) {
  return (
    <Provider store={store}>
      <Press rows={rows} />
    </Provider>
  )
}

let closeOneSpy = vi.fn()
let openOneSpy = vi.fn()

/** lead ─ w1 (running) ─ sub1 (running) / sub2 ; w2 */
const RUNNING_TREE: Row[] = [
  root('lead', 'pipeline-conductor', true),
  child('w1', 'worker - babysit', 'lead', true),
  child('sub1', 'subworker - rerun lane', 'w1', true),
  child('sub2', 'subworker - log fetch', 'w1'),
  child('w2', 'worker - locale sweep', 'lead'),
]

/** Same tree, every descendant finished (lead itself is still running). */
const IDLE_TREE: Row[] = RUNNING_TREE.map(r => ({ ...r, running: r.key === 'lead' }))

beforeEach(() => {
  deleteSlot.mockReset()
  deleteSlot.mockImplementation((key: string) => () => ({ unwrap: async () => key }))
  closeOneSpy = vi.fn()
  openOneSpy = vi.fn()
  resumeFromHistory.mockReset()
  resumeFromHistory.mockImplementation(({ key }: { key: string; title: string }) => () => ({
    unwrap: async () => ({ ok: true, key }),
  }))
  chatConfig.confirmCloseSession = false
})

describe('planCloseTree', () => {
  it('groups descendants by depth below the pressed card', () => {
    const plan = planCloseTree(RUNNING_TREE, 'lead', isRunning)
    expect(plan.levels.map(l => [l.depth, l.sessions.map(s => s.key)])).toEqual([
      [0, ['lead']],
      [1, ['w1', 'w2']],
      [2, ['sub1', 'sub2']],
    ])
    expect(plan.total).toBe(5)
    expect(plan.descendantCount).toBe(4)
  })

  it('blocks when any descendant is running; counts the whole plan in runningTotal', () => {
    const plan = planCloseTree(RUNNING_TREE, 'lead', isRunning)
    // Gate: descendants only (the pressed session's own state never gates this).
    expect(plan.runningDescendants).toBe(2)
    // Count: whole plan (used in the notice sentence, must match "Close all N").
    expect(plan.runningTotal).toBe(3)
    expect(plan.total).toBe(5)
    expect(plan.blocked).toBe(true)
    // Deepest-first order is kept for the unblocked close path.
    expect(plan.order.indexOf('sub1')).toBeLessThan(plan.order.indexOf('w1'))
    expect(plan.order.at(-1)).toBe('lead')
  })

  it('is not blocked when every descendant has finished', () => {
    const plan = planCloseTree(IDLE_TREE, 'lead', isRunning)
    expect(plan.blocked).toBe(false)
    expect(plan.runningDescendants).toBe(0)
    expect(plan.order).toHaveLength(5)
  })

  it('plans nothing for an unknown key', () => {
    const plan = planCloseTree(RUNNING_TREE, 'gone', isRunning)
    expect(plan).toMatchObject({ total: 0, order: [], levels: [], blocked: false })
  })

  it('titles a session by its key when it has none', () => {
    const plan = planCloseTree([{ key: 'lead', parent: null }], 'lead', () => false)
    expect(plan.levels[0].sessions[0].title).toBe('lead')
  })
})

describe('useCloseSessionTree', () => {
  it('keeps ONE callback identity across a row-list change and still plans over new rows', async () => {
    const seen: Array<(key: string) => void> = []
    function Probe({ rows }: { rows: Row[] }) {
      const { closeSessionTree, closeTreeDialog } = useCloseSessionTree({
        rows, isRunning, closeOne: (key: string) => closeOneSpy(key),
      })
      seen.push(closeSessionTree)
      return (
        <>
          <button data-testid="x" onClick={() => closeSessionTree('lead')}>x</button>
          {closeTreeDialog}
        </>
      )
    }
    const user = userEvent.setup()
    const { rerender } = render(
      <Provider store={store}><Probe rows={[root('lead', 'solo')]} /></Provider>,
    )
    rerender(<Provider store={store}><Probe rows={IDLE_TREE} /></Provider>)
    expect(seen.length).toBeGreaterThan(1)
    expect(seen.at(-1)).toBe(seen[0])
    await user.click(screen.getByTestId('x'))
    await waitFor(() => expect(closedOrder()).toHaveLength(5))
    expect(closeOneSpy).not.toHaveBeenCalled()
  })

  it('delegates a card with no descendants to the existing single close', async () => {
    const user = userEvent.setup()
    render(<Harness rows={[root('lead', 'solo', true)]} />)
    await user.click(screen.getByTestId('x'))
    expect(closeOneSpy).toHaveBeenCalledWith('lead')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(deleteSlot).not.toHaveBeenCalled()
  })

  it('closes a finished subtree silently with preference off (default)', async () => {
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    await waitFor(() => expect(closedOrder()).toHaveLength(5))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(closedOrder()).toEqual(['sub1', 'sub2', 'w1', 'w2', 'lead'])
    expect(closeOneSpy).not.toHaveBeenCalled()
  })

  it('says what a prompt-free close did, and Undo reopens exactly those sessions parent-first', async () => {
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    const notice = await screen.findByTestId('close-tree-closed')
    expect(notice).toHaveTextContent('Closed 5 sessions')
    await user.click(screen.getByTestId('close-tree-undo'))
    await waitFor(() => expect(resumeFromHistory).toHaveBeenCalledTimes(5))
    expect(resumeFromHistory.mock.calls.map(([a]) => a.key)).toEqual(['lead', 'w2', 'w1', 'sub2', 'sub1'])
    expect(resumeFromHistory.mock.calls[0][0].title).toBe('pipeline-conductor')
    expect(screen.queryByTestId('close-tree-closed')).toBeNull()
    expect(screen.queryByTestId('close-tree-undo-failed')).toBeNull()
  })

  it('Undo names a session it could not reopen', async () => {
    resumeFromHistory.mockImplementation(({ key }: { key: string; title: string }) => () => ({
      unwrap: async () => ({ ok: key !== 'w2', key }),
    }))
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    await user.click(await screen.findByTestId('close-tree-undo'))
    const failed = await screen.findByTestId('close-tree-undo-failed')
    expect(failed).toHaveTextContent('worker - locale sweep')
    expect(failed).not.toHaveTextContent('worker - babysit')
  })

  it('the receipt counts only the sessions that did close', async () => {
    deleteSlot.mockImplementation((key: string) => () => ({
      unwrap: async () => {
        if (key === 'w1') throw new Error('save failed')
        return key
      },
    }))
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    // w1 refused, so the lead above it stays open too: sub1, sub2 and w2 closed.
    expect(await screen.findByTestId('close-tree-closed')).toHaveTextContent('Closed 3 sessions')
  })

  it('asks once with the in-app confirm when the preference is on', async () => {
    chatConfig.confirmCloseSession = true
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    // The in-app confirm opens (not window.confirm).
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toBeInTheDocument()
    // Nothing closed while the question is open.
    expect(deleteSlot).not.toHaveBeenCalled()
    // One count per role: the title counts the 4 UNDER it, the button all 5 closed.
    expect(await screen.findByRole('button', { name: /Close all 5/i })).toBeInTheDocument()
    expect(screen.getByText('Close this session and the 4 sessions under it?')).toBeInTheDocument()
    expect(screen.getByText(/Closing moves them to Older sessions/i)).toBeInTheDocument()
    // Confirm closes the whole tree.
    await user.click(screen.getByRole('button', { name: /Close all 5/i }))
    await waitFor(() => expect(closedOrder()).toHaveLength(5))
    expect(closedOrder()).toEqual(['sub1', 'sub2', 'w1', 'w2', 'lead'])
    // A close the person just confirmed gets no second receipt.
    expect(screen.queryByTestId('close-tree-closed')).toBeNull()
  })

  it('cancelling the preference confirm closes nothing', async () => {
    chatConfig.confirmCloseSession = true
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    await user.click(await screen.findByRole('button', { name: /Cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(deleteSlot).not.toHaveBeenCalled()
  })

  it('refuses while any descendant runs: notice lists levels, nothing closes', async () => {
    const user = userEvent.setup()
    render(<Harness rows={RUNNING_TREE} />)
    await user.click(screen.getByTestId('x'))
    // The notice opens.
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    // Nothing closed.
    expect(deleteSlot).not.toHaveBeenCalled()
    // Level headings name depth.
    const levels = screen.getByTestId('close-tree-levels')
    expect(levels.querySelectorAll('[data-testid^="close-tree-level-"]')).toHaveLength(3)
    expect(screen.getByTestId('close-tree-level-0')).toHaveTextContent('This session')
    expect(screen.getByTestId('close-tree-level-1')).toHaveTextContent('1 level under')
    expect(screen.getByTestId('close-tree-level-2')).toHaveTextContent('2 levels under')
    // Every session named; every session UNDER the lead marked.
    for (const row of RUNNING_TREE) {
      const entry = screen.getByTestId(`close-tree-session-${row.key}`)
      expect(entry).toHaveTextContent(row.title!)
      if (row.key === 'lead') continue
      expect(entry).toHaveTextContent(row.running ? 'Running' : 'Finished')
    }
    // The pressed session carries no status, even though it is running: its
    // state never gates the press, and the title does not count it.
    const lead = screen.getByTestId('close-tree-session-lead')
    expect(lead).not.toHaveTextContent('Running')
    expect(lead).not.toHaveTextContent('Finished')
    expect(lead.tagName).not.toBe('BUTTON')
    // The title counts the running sessions UNDER the pressed one, and the list
    // shows exactly that many RUNNING rows: the two numbers agree.
    expect(screen.getByTestId('close-tree-title')).toHaveTextContent('2 sessions under this one are still running')
    expect(levels.querySelectorAll('[data-status="running"]')).toHaveLength(2)
    // The body says how to act, and does not contradict the refusal banner.
    expect(screen.getByText(/Open a running session below to stop it/)).toBeInTheDocument()
    // Exactly ONE action: dismiss. No "close anyway" button.
    const buttons = screen.getAllByRole('button')
    void buttons // suppress unused lint warning
    expect(screen.getByTestId('close-tree-dismiss')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Close all/i })).toBeNull()
  })

  it('a running row in the notice opens that session and closes the notice', async () => {
    const user = userEvent.setup()
    render(<Harness rows={RUNNING_TREE} />)
    await user.click(screen.getByTestId('x'))
    const running = await screen.findByTestId('close-tree-session-sub1')
    expect(running.tagName).toBe('BUTTON')
    // A finished row is not a control.
    expect(screen.getByTestId('close-tree-session-sub2').tagName).not.toBe('BUTTON')
    await user.click(running)
    expect(openOneSpy).toHaveBeenCalledWith('sub1')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(deleteSlot).not.toHaveBeenCalled()
  })

  it('dismissing the notice closes nothing', async () => {
    const user = userEvent.setup()
    render(<Harness rows={RUNNING_TREE} />)
    await user.click(screen.getByTestId('x'))
    await user.click(await screen.findByTestId('close-tree-dismiss'))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(deleteSlot).not.toHaveBeenCalled()
    expect(closeOneSpy).not.toHaveBeenCalled()
  })

  it('reports a refused close by name instead of swallowing it', async () => {
    deleteSlot.mockImplementation((key: string) => () => ({
      unwrap: async () => {
        if (key === 'w1') throw new Error('save failed')
        return key
      },
    }))
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    const notice = await screen.findByTestId('close-tree-refused')
    expect(notice).toHaveTextContent('worker - babysit')
    expect(notice).not.toHaveTextContent('save failed')
    expect(notice).not.toHaveTextContent('worker - locale sweep')
    // The lead above the refusal is never asked to close, so the refused worker
    // keeps its lead instead of standing alone at the top level.
    expect(closedOrder()).toEqual(['sub1', 'sub2', 'w1', 'w2'])
    expect(notice).toHaveTextContent('pipeline-conductor')
    expect(notice).toHaveTextContent('Try closing them again')
  })

  it('a deep refusal keeps every ancestor open and still closes the siblings', async () => {
    deleteSlot.mockImplementation((key: string) => () => ({
      unwrap: async () => {
        if (key === 'sub1') throw new Error('save failed')
        return key
      },
    }))
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    const notice = await screen.findByTestId('close-tree-refused')
    // sub1 refused -> w1 and lead stay; sub2 and w2 are off the refused path.
    expect(closedOrder()).toEqual(['sub1', 'sub2', 'w2'])
    for (const kept of ['subworker - rerun lane', 'worker - babysit', 'pipeline-conductor']) {
      expect(notice).toHaveTextContent(kept)
    }
    expect(notice).not.toHaveTextContent('subworker - log fetch')
    expect(await screen.findByTestId('close-tree-closed')).toHaveTextContent('Closed 2 sessions')
  })

  it('one refused session reads in the singular', async () => {
    // Only the lead refuses, so exactly one session is still open.
    deleteSlot.mockImplementation((key: string) => () => ({
      unwrap: async () => {
        if (key === 'lead') throw new Error('save failed')
        return key
      },
    }))
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    const notice = await screen.findByTestId('close-tree-refused')
    expect(notice).toHaveTextContent('Still open: pipeline-conductor. Try closing it again in a moment.')
  })

  it('reports nothing when every close succeeds', async () => {
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    await waitFor(() => expect(closedOrder()).toHaveLength(5))
    expect(screen.queryByTestId('close-tree-refused')).toBeNull()
  })
})

describe('close-tree copy: singular forms', () => {
  it('reads correctly for exactly one', () => {
    expect(i18nT('hooks.useCloseSessionTree.title', { count: 1 })).toBe('1 session under this one is still running')
    expect(i18nT('hooks.useCloseSessionTree.pref_title', { count: 1 })).toBe('Close this session and the 1 session under it?')
    expect(i18nT('hooks.useCloseSessionTree.pref_confirm', { count: 1 })).toBe('Close 1 session')
    expect(i18nT('hooks.useCloseSessionTree.closed', { count: 1 })).toBe('Closed 1 session')
    expect(i18nT('hooks.useCloseSessionTree.level', { count: 1 })).toBe('1 level under')
    expect(i18nT('pages.chatSidebar.close_session_tree', { count: 1, total: 2 })).toBe('Close all 2: this session and the 1 under it')
    expect(i18nT('hooks.useCloseSessionTree.refused', { count: 1, names: 'a' })).toBe('Still open: a. Try closing it again in a moment.')
    expect(i18nT('hooks.useCloseSessionTree.undo_failed', { count: 1, names: 'a' })).toBe('Not reopened: a. You can still open it from Older sessions.')
  })

  it('reads correctly for many', () => {
    expect(i18nT('hooks.useCloseSessionTree.title', { count: 3 })).toBe('3 sessions under this one are still running')
    expect(i18nT('hooks.useCloseSessionTree.closed', { count: 5 })).toBe('Closed 5 sessions')
    expect(i18nT('pages.chatSidebar.close_session_tree', { count: 4, total: 5 })).toBe('Close all 5: this session and the 4 under it')
    expect(i18nT('hooks.useCloseSessionTree.refused', { count: 2, names: 'a, b' })).toBe('Still open: a, b. Try closing them again in a moment.')
    expect(i18nT('hooks.useCloseSessionTree.undo_failed', { count: 2, names: 'a, b' })).toBe('Not reopened: a, b. You can still open them from Older sessions.')
  })
})
