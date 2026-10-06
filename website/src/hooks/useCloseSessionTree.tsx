import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'

import ErrorNotice from '../components/ErrorNotice'
import Modal from '../components/Modal'
import { Btn } from '../components/ui'
import { useConfirm } from '../components/ConfirmDialog'
import { useAppDispatch } from '../store'
import { deleteSlot, resumeFromHistory } from '../store/chatSlice'
import { loadChatConfig } from '../pages/chat/ChatSettings'
import { i18nT } from '../i18n/t'
import { planCloseTree, type CloseTreePlan, type CloseTreeRow } from '../lib/sessionCloseTree'

/**
 * The session tree's ✕, as a close over the whole subtree that REFUSES while any
 * of it is still working (#17253).
 *
 * The card's ✕ takes down the session AND the sessions nested under it, so one press
 * on a lead could end several workers' turns at once, with nothing to undo. So the
 * press has two outcomes and no third:
 *
 *   - nothing under the card is running -> the whole subtree closes, no prompt;
 *   - anything under it is running      -> nothing closes, and a notice lists the
 *                                          sub-levels and marks which are running.
 *
 * There is deliberately no "close anyway" on that notice. A turn cancelled by a
 * tidy-up click cannot be restored, and a button that does it anyway is the same
 * one-click loss with a dialog in front of it. The person stops or finishes those
 * sessions first, then presses ✕ again.
 *
 * The notice is the in-app `Modal`, never `window.confirm` / `alert` (CREW-20787 /
 * #15976): the native sheet is synchronous, unthemeable, and cannot list the
 * sessions that are still running, which is the whole point of showing it.
 */

/** The levels, with each session marked running or finished. */
function CloseTreeLevels({ plan, onOpen }: { plan: CloseTreePlan; onOpen: (key: string) => void }) {
  return (
    <div
      className="max-h-[40vh] overflow-y-auto rounded-lg border border-border bg-bg-elevated"
      data-testid="close-tree-levels"
    >
      {plan.levels.map(level => (
        <div
          key={level.depth}
          className="px-2.5 py-1.5 border-b border-border last:border-b-0"
          data-testid={`close-tree-level-${level.depth}`}
        >
          <div className="font-mono text-[10px] font-semibold uppercase tracking-wider text-muted">
            {level.depth === 0
              ? i18nT('hooks.useCloseSessionTree.level_self')
              : i18nT('hooks.useCloseSessionTree.level', { count: level.depth })}
          </div>
          {level.sessions.map(session => {
            // The pressed session carries NO status: its own running state never
            // gates the press, and the title counts only the sessions under it.
            // Tagging it RUNNING put one more running row in the list than the
            // title's count, and the reader stopped at the mismatch.
            const self = level.depth === 0
            const running = !self && session.running
            // A RUNNING row is a button that opens that session: the notice asks the
            // person to stop or finish it, and the session itself is where its Stop
            // control lives. A finished row has nothing to act on, so it stays text.
            const Row = running ? 'button' : 'div'
            return (
            <Row
              key={session.key}
              {...(running ? {
                type: 'button' as const,
                onClick: () => onOpen(session.key),
                title: i18nT('hooks.useCloseSessionTree.open_running'),
              } : {})}
              className={`flex w-full items-center gap-2 pt-0.5 text-left ${
                running ? 'cursor-pointer rounded hover:bg-bg-hover focus-ring' : ''
              }`}
              data-testid={`close-tree-session-${session.key}`}
              {...(self ? {} : { 'data-status': running ? 'running' : 'finished' })}
            >
              {/* The ACCENT, the same hue the lane behind this notice paints a
                  running row's status line with, so the two never read as two
                  different states for one session. */}
              {self ? null : (
                <span
                  className={`w-1.5 h-1.5 rounded-full shrink-0 ${running ? 'bg-accent' : 'bg-muted-strong'}`}
                  aria-hidden="true"
                />
              )}
              <span className={`text-[12.5px] truncate min-w-0 ${running ? 'text-accent underline underline-offset-2' : 'text-text'}`}>{session.title}</span>
              {self ? null : (
                <span
                  className={`ml-auto shrink-0 font-mono text-[10px] font-semibold uppercase tracking-wider ${
                    running ? 'text-accent' : 'text-muted-strong'
                  }`}
                >
                  {i18nT(running
                    ? 'hooks.useCloseSessionTree.running'
                    : 'hooks.useCloseSessionTree.finished')}
                </span>
              )}
            </Row>
            )
          })}
        </div>
      ))}
    </div>
  )
}

export interface CloseSessionTreeOptions<R extends CloseTreeRow> {
  /** The rows currently on screen, in the lane's own order, keyed by slot key. */
  rows: readonly R[]
  /** The caller's own "still working" predicate — see `planCloseTree`. */
  isRunning: (row: R) => boolean
  /**
   * Today's single-session close, used verbatim for a card with no descendants.
   *
   * Delegated rather than reimplemented so a leaf card keeps the exact behaviour it
   * has now, including the `confirmCloseSession` preference's own prompt. Nothing
   * about a session with nothing under it changed in #17253.
   */
  closeOne: (key: string) => void
  /**
   * Open one session, the way a press on its row does. The still-running notice
   * makes each RUNNING session a link to it, because that session is where the
   * person can stop it. Optional so a surface with no session view keeps the
   * rows as plain text.
   */
  openOne?: (key: string) => void
}

/** How long the "Closed N sessions · Undo" notice stays up. */
export const CLOSE_TREE_UNDO_MS = 15000

export interface CloseSessionTree {
  /** Close *key* and its subtree — or refuse, with a notice, while any of it runs. */
  closeSessionTree: (key: string) => void
  /** The still-running notice. Render once in the owning component's JSX. */
  closeTreeDialog: ReactNode
  /**
   * The sessions a subtree close could NOT close, or nothing.
   *
   * The endpoint refuses a close while a guarded history write is still running,
   * and asks for it to be retried in a moment. The only other trace of that is the
   * row staying in the list, which reads as the press having missed. Render it
   * where the surface puts its own failures.
   */
  closeTreeError: ReactNode
  /**
   * "Closed N sessions" with Undo, after a tree close that ran without a prompt.
   * Undo reopens exactly the closed sessions through the same resume path the
   * Older sessions list uses. Render it in the same band as `closeTreeError`.
   */
  closeTreeNotice: ReactNode
}

export function useCloseSessionTree<R extends CloseTreeRow>(
  { rows, isRunning, closeOne, openOne }: CloseSessionTreeOptions<R>,
): CloseSessionTree {
  const dispatch = useAppDispatch()
  const { confirm, confirmDialog: prefConfirmDialog } = useConfirm()
  /** The plan a press refused, while its notice is open. */
  const [blocked, setBlocked] = useState<CloseTreePlan | null>(null)
  /** Kept through the notice's exit animation: `Modal` stays mounted with
   *  `open=false` so it can play out, and still needs its contents to do so. */
  const lastBlocked = useRef<CloseTreePlan | null>(null)
  /** Titles of the sessions the last subtree close could not close. */
  const [refusal, setRefusal] = useState<string[] | null>(null)
  /** Titles of the sessions an Undo could not reopen. */
  const [undoRefusal, setUndoRefusal] = useState<string[] | null>(null)
  /** The sessions the last prompt-free tree close closed, parent-first, while
   *  its Undo offer is up. */
  const [closed, setClosed] = useState<{ key: string; title: string }[] | null>(null)

  // The inputs are read through a ref so `closeSessionTree` has a STABLE identity.
  // `rows` is the live slot list, so it is a new array on every slot push: as a
  // dependency it would make this callback new on every push, and the sidebar
  // passes it into each row's memoized menu props. That bust the row memo
  // boundary wholesale — one insertion re-rendered 21 rows instead of 1 and
  // rebuilt every row's menus (`ChatSidebar.rowMemo.test.tsx`). A press reads the
  // ref at call time, so it still plans over the rows on screen at that moment.
  const inputs = useRef({ rows, isRunning, closeOne, openOne })
  inputs.current = { rows, isRunning, closeOne, openOne }

  const closeSessionTree = useCallback((key: string) => {
    const { rows: liveRows, isRunning: live, closeOne: closeThisOne } = inputs.current
    const plan = planCloseTree(liveRows, key, live)
    // A press on a row that has since left the list closes nothing.
    if (plan.total === 0) return
    if (plan.descendantCount === 0) {
      closeThisOne(key)
      return
    }
    // Anything under the card still working: refuse, and say which. Nothing is
    // dispatched on this path at all, so there is no partial state to undo.
    if (plan.blocked) {
      lastBlocked.current = plan
      setBlocked(plan)
      return
    }
    const run = async () => {
      // Before closing, honour the user's "confirm before closing a session"
      // preference. The preference applies to the whole tree as one question:
      // one ask for all N sessions is what the person opted into, not one per
      // row. `useConfirm` is the in-app dialog, never `window.confirm` (the
      // native sheet is synchronous and cannot name what it is about to close).
      const asked = loadChatConfig().confirmCloseSession
      if (asked) {
        // One count per role, the same everywhere: the title counts the sessions
        // UNDER this one, the button counts everything the press closes.
        const confirmed = await confirm({
          title: i18nT('hooks.useCloseSessionTree.pref_title', { count: plan.descendantCount }),
          body: i18nT('hooks.useCloseSessionTree.pref_body'),
          confirmLabel: i18nT('hooks.useCloseSessionTree.pref_confirm', { count: plan.total }),
        })
        if (!confirmed) return
      }
      // Re-check the running state after any asynchronous wait (the confirm dialog
      // above). While the dialog was open, another window could have sent a message
      // to an idle descendant, making it running. Proceeding on the pre-dialog
      // captured plan would close a session that the guard exists to protect.
      const { rows: liveRows, isRunning: liveRunning } = inputs.current
      const recheck = planCloseTree(liveRows, plan.order.at(-1) ?? '', liveRunning)
      if (recheck.blocked) {
        // State changed during the dialog: redirect to the refusal notice.
        lastBlocked.current = recheck
        setBlocked(recheck)
        return
      }
      // Sequential, because `plan.order` is deepest-first and that ordering only
      // holds if each close completes before the next begins: no parent is
      // archived while a child still cites it as a live creator.
      //
      // A refused close leaves that one session open and does not abort the rest,
      // and it is REPORTED: its only other trace is the row staying put, and the
      // store's rejected-close path releases the hold without telling anyone.
      const titleOf = new Map(
        plan.levels.flatMap(level => level.sessions.map(s => [s.key, s.title] as const)),
      )
      // Names only. `deleteSlot` rejects with a fixed `save failed` whatever the
      // server said, so forwarding the thunk's message would print a cause that
      // is not the cause.
      const refused: string[] = []
      const done: { key: string; title: string }[] = []
      // Every ancestor of a refused close stays open. `plan.order` is deepest
      // first, so a child's outcome is known before its parent comes up: closing
      // the parent anyway would leave the refused worker alone at the top level,
      // its lead gone, which is the orphan this whole guard exists to prevent.
      const keepOpen = new Set<string>()
      const keepAncestors = (slot: string) => {
        const parent = plan.parentOf.get(slot)
        if (parent != null) keepOpen.add(parent)
      }
      setClosed(null)
      setUndoRefusal(null)
      for (const slot of plan.order) {
        if (keepOpen.has(slot)) {
          // Named with the refusals: it is still open, and the person's next
          // press on the lead is what closes it once the refused one goes.
          refused.push(titleOf.get(slot) ?? slot)
          keepAncestors(slot)
          continue
        }
        try {
          await dispatch(deleteSlot(slot)).unwrap()
          done.push({ key: slot, title: titleOf.get(slot) ?? slot })
        } catch {
          refused.push(titleOf.get(slot) ?? slot)
          keepAncestors(slot)
        }
      }
      setRefusal(refused.length > 0 ? refused : null)
      // A close nobody was asked about gets a receipt and an Undo: five sessions
      // leaving the lane on one press with no trace reads as lost work. A close
      // the person just confirmed in a dialog needs no second receipt.
      // Stored parent-first (the reverse of the close order), the order Undo
      // reopens them in, so a worker never comes back before its lead.
      if (!asked && done.length > 0) setClosed(done.reverse())
    }
    void run()
    // `dispatch`, `confirm`, and both setters are stable, so this callback is created once.
  }, [dispatch, confirm])

  // The Undo offer expires: it belongs to the press that just happened, and a
  // late Undo would bring back sessions the person has long stopped expecting.
  useEffect(() => {
    if (!closed) return
    const t = setTimeout(() => setClosed(null), CLOSE_TREE_UNDO_MS)
    return () => clearTimeout(t)
  }, [closed])

  /** Reopen exactly the sessions the last close closed, through the same resume
   *  the Older sessions list runs on a press. Parent-first, one at a time. */
  const undo = useCallback(async (sessions: { key: string; title: string }[]) => {
    setClosed(null)
    const failed: string[] = []
    for (const s of sessions) {
      try {
        const r = await dispatch(resumeFromHistory({ key: s.key, title: s.title })).unwrap()
        if (!r.ok) failed.push(s.title)
      } catch {
        failed.push(s.title)
      }
    }
    setUndoRefusal(failed.length > 0 ? failed : null)
  }, [dispatch])

  const openFromNotice = useCallback((key: string) => {
    setBlocked(null)
    inputs.current.openOne?.(key)
  }, [])

  const shown = blocked ?? lastBlocked.current
  const closeTreeDialog = (
    <>
      {shown ? (
        <Modal
          open={!!blocked}
          onClose={() => setBlocked(null)}
          // Wraps instead of truncating: on a phone the header is narrower than
          // the sentence, and an ellipsis cut the one line saying why.
          title={
            <span className="block whitespace-normal leading-snug [overflow-wrap:anywhere]" data-testid="close-tree-title">
              {i18nT('hooks.useCloseSessionTree.title', { count: shown.runningDescendants })}
            </span>
          }
          maxWidth={440}
          // One action, and it closes nothing: there is no "close anyway" here.
          footer={
            <Btn primary onClick={() => setBlocked(null)} data-testid="close-tree-dismiss">
              {i18nT('hooks.useCloseSessionTree.dismiss')}
            </Btn>
          }
        >
          <p className="text-sm text-text m-0 mb-3">
            {i18nT('hooks.useCloseSessionTree.body')}
          </p>
          <CloseTreeLevels plan={shown} onOpen={openFromNotice} />
        </Modal>
      ) : null}
      {/* Preference confirm — only mounted when the pref is on and the tree is
          not blocked. `useConfirm` keeps it null until a press triggers it. */}
      {prefConfirmDialog}
    </>
  )

  const closeTreeError = (
    <ErrorNotice
      title={i18nT('hooks.useCloseSessionTree.refused_title')}
      // Named, not counted: which sessions are still open is the thing the person
      // has to go back to, and a bare number would send them to compare lists.
      message={refusal ? i18nT('hooks.useCloseSessionTree.refused', { count: refusal.length, names: refusal.join(', ') }) : null}
      messagePlacement="below"
      // A hand-off loses nothing here: this surface holds no draft, and the
      // sessions that did close are already gone.
      askAgent
      // Dismissable: a refusal is a moment, and pressing the row's own close
      // again is the retry the server asked for.
      onDismiss={() => setRefusal(null)}
      className="mx-2 mt-2 shrink-0"
      testId="close-tree-refused"
    />
  )

  const closeTreeNotice = (
    <>
      {closed ? (
        <div
          role="status"
          aria-live="polite"
          className="mx-2 mt-2 shrink-0 flex items-center gap-2 rounded-md border border-border bg-accent-subtle px-2.5 py-1.5 text-[12.5px] text-text"
          data-testid="close-tree-closed"
        >
          <span className="min-w-0 flex-1">
            {i18nT('hooks.useCloseSessionTree.closed', { count: closed.length })}
          </span>
          <button
            type="button"
            onClick={() => { void undo(closed) }}
            className="shrink-0 cursor-pointer rounded-[5px] border border-border-strong bg-transparent px-1.5 py-px text-[12px] text-accent hover:bg-bg-hover focus-ring"
            data-testid="close-tree-undo"
          >
            {i18nT('hooks.useCloseSessionTree.undo')}
          </button>
        </div>
      ) : null}
      <ErrorNotice
        title={i18nT('hooks.useCloseSessionTree.undo_failed_title')}
        message={undoRefusal ? i18nT('hooks.useCloseSessionTree.undo_failed', { count: undoRefusal.length, names: undoRefusal.join(', ') }) : null}
        messagePlacement="below"
        // A hand-off loses nothing here, the same call as the refused notice above:
        // this band holds no draft, and the sessions that did not reopen are still
        // safe in Older sessions.
        askAgent
        onDismiss={() => setUndoRefusal(null)}
        className="mx-2 mt-2 shrink-0"
        testId="close-tree-undo-failed"
      />
    </>
  )

  return { closeSessionTree, closeTreeDialog, closeTreeError, closeTreeNotice }
}
