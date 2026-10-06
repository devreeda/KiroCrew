import { useQuery } from '@tanstack/react-query'
import { Server } from 'lucide-react'
import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { openCrewWindow } from './crew-window/crewWindowStore'
import { i18nT } from '../../i18n/t'
import { errMessage } from '../../utils/thunkError'

/**
 * The one line above the disabled composer of a chat that ran on a crew.
 *
 * Such a slot (`executor === 'remote'`) is a read-only archive: its turns ran on
 * the peer, and the peer's own session is where the conversation continues. The
 * name comes from the SHARED `['instances']` cache the sidebar chip reads, and
 * falls back to the instance id so the line never names a blank crew. A failed
 * lookup is shown, except the expected 403 of an install with crews turned off.
 *
 * The peer's own slot key reaches the client only inside `row_identity`
 * (`<instance_id>:<peer_key>`), so the open button reads it from there and is
 * absent when the row carries no such identity.
 */
export function RelayArchiveNotice({ instanceId, rowIdentity }: { instanceId: string; rowIdentity?: string }) {
  const prefix = `${instanceId}:`
  const peerKey = rowIdentity?.startsWith(prefix) ? rowIdentity.slice(prefix.length) : ''
  const { data, error } = useQuery({ queryKey: ['instances'], queryFn: () => api.listInstances() })
  const name = data?.instances?.find(i => i.id === instanceId)?.name || instanceId
  const crewsOff = error instanceof ApiError && error.status === 403 && /disabled/i.test(error.message)
  return (
    <div className="px-4 mb-1.5 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <div className="flex items-center gap-1.5 text-[12px] text-muted-strong" role="status" data-testid="relay-archive-notice">
        <Server size={12} className="shrink-0" aria-hidden="true" />
        <span>{i18nT('pages.chat.relayArchive.notice', { name })}</span>
        {peerKey && (
          <Btn className="shrink-0" data-testid="relay-archive-open" onClick={() => openCrewWindow({ instanceId, key: peerKey })}>
            {i18nT('pages.chat.relayArchive.open', { name })}
          </Btn>
        )}
      </div>
      {error && !crewsOff && <ErrorNotice className="mt-1.5" message={errMessage(error)} askAgent testId="relay-archive-lookup-error" />}
    </div>
  )
}
