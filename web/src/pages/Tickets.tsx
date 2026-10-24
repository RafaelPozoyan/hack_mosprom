import { useQuery, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { CheckCircle2, Inbox, Sparkles } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { api, fmtDateTime, type Ticket } from '../api'
import { ResponseLine } from '../components/ResponseTime'
import { Avatar, Badge, Btn, Card, Empty, TICKET_KIND, inputCls, siteName, timeAgo } from '../components/ui'

export default function Tickets() {
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const [status, setStatus] = useState<'open' | 'closed'>('open')
  const { data: list = [] } = useQuery({
    queryKey: ['tickets', status],
    queryFn: () => api.get<Ticket[]>(`/api/tickets?status=${status}`),
    refetchInterval: 10_000,
  })
  const selected = Number(params.get('id')) || list[0]?.id

  useEffect(() => {
    const es = api.stream('/api/feed')
    es.onmessage = () => {
      qc.invalidateQueries({ queryKey: ['tickets'] })
      qc.invalidateQueries({ queryKey: ['ticket'] })
    }
    return () => es.close()
  }, [qc])

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-5">
      <h1 className="text-2xl font-semibold tracking-tight">Обращения новичков</h1>
      <div className="grid gap-5 lg:grid-cols-[360px_1fr]">
        <Card
          action={
            <div className="flex rounded-xl bg-ink/[0.05] p-0.5 text-[13px]">
              {(['open', 'closed'] as const).map((s) => (
                <button
                  key={s}
                  onClick={() => setStatus(s)}
                  className={clsx('rounded-lg px-3 py-1', status === s && 'bg-white font-medium shadow-sm')}
                >
                  {s === 'open' ? 'Открытые' : 'Решённые'}
                </button>
              ))}
            </div>
          }
          title={status === 'open' ? `Ждут ответа: ${list.length}` : 'Решённые'}
        >
          {list.length === 0 ? (
            <Empty icon={<CheckCircle2 className="text-lime-2" />}>
              {status === 'open' ? 'Все обращения разобраны' : 'Пока пусто'}
            </Empty>
          ) : (
            <div className="-mx-2 flex flex-col">
              {list.map((t) => (
                <button
                  key={t.id}
                  onClick={() => setParams({ id: String(t.id) })}
                  className={clsx(
                    'flex gap-3 rounded-xl px-2 py-3 text-left transition',
                    t.id === selected ? 'bg-ink/[0.05]' : 'hover:bg-ink/[0.025]',
                  )}
                >
                  <Avatar name={t.employee} size={34} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium">{t.employee}</span>
                      <span className="shrink-0 text-[11px] text-ink/40">{timeAgo(t.created_at)}</span>
                    </div>
                    <div className="mt-0.5 flex items-center gap-1.5">
                      <Badge tone={TICKET_KIND[t.kind]?.tone}>{TICKET_KIND[t.kind]?.label}</Badge>
                      {t.response?.overdue && <Badge tone="red">просрочено</Badge>}
                      <span className="truncate text-xs text-ink/55">{t.subject}</span>
                    </div>
                  </div>
                </button>
              ))}
            </div>
          )}
        </Card>
        {selected ? (
          <TicketView id={selected} />
        ) : (
          <Card>
            <Empty icon={<Inbox className="text-ink/30" />}>Выберите обращение слева</Empty>
          </Card>
        )}
      </div>
    </div>
  )
}

function TicketView({ id }: { id: number }) {
  const qc = useQueryClient()
  const { data: t } = useQuery({
    queryKey: ['ticket', id],
    queryFn: () => api.get<Ticket>(`/api/tickets/${id}`),
  })
  const [text, setText] = useState('')
  if (!t) return null
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['tickets'] })
    qc.invalidateQueries({ queryKey: ['ticket', id] })
  }
  const reply = async () => {
    if (!text.trim()) return
    await api.post(`/api/tickets/${id}/reply`, { text, author: 'HR' })
    setText('')
    refresh()
  }
  const close = async () => {
    await api.post(`/api/tickets/${id}/close`)
    refresh()
  }
  const preparing = t.summary.startsWith('ИИ готовит')
  return (
    <Card
      title={
        <Link to={`/employees/${t.employee_id}`} className="hover:underline">
          {t.employee}
        </Link>
      }
      subtitle={`${siteName(t.site)} · ${fmtDateTime(t.created_at)}`}
      action={
        t.status === 'open' && (
          <Btn variant="ghost" small onClick={close}>
            <CheckCircle2 size={14} /> Решено
          </Btn>
        )
      }
    >
      <ResponseLine t={t} />
      <div className="mb-4 rounded-2xl bg-lime/15 p-4 text-sm">
        <div className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-ink/60">
          <Sparkles size={14} /> Суть обращения — подготовил Корсик
        </div>
        <div className={clsx('whitespace-pre-wrap', preparing && 'text-ink/50')}>
          {preparing ? 'Корсик собирает контекст переписки…' : t.summary}
        </div>
      </div>
      <div className="flex max-h-[440px] flex-col gap-2 overflow-y-auto rounded-2xl bg-ink/[0.03] p-3">
        {t.messages?.map((m) => (
          <div
            key={m.id}
            className={clsx(
              'max-w-[80%] rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap',
              m.author === 'employee'
                ? 'self-end bg-[#d9f2a6]'
                : m.author === 'hr'
                  ? 'bg-sky-100'
                  : 'bg-white',
            )}
          >
            <div className="mb-0.5 text-[11px] text-ink/45">
              {m.author === 'employee' ? t.employee : m.author === 'hr' ? 'HR' : 'Корсик'} ·{' '}
              {fmtDateTime(m.created_at)}
            </div>
            {m.text}
          </div>
        ))}
      </div>
      {t.status === 'open' ? (
        <>
          <div className="mt-3 flex gap-2">
            <input
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && reply()}
              placeholder="Напишите ответ — он придёт новичку в чат с Корсиком"
              className={clsx(inputCls, 'flex-1')}
            />
            <Btn onClick={reply} disabled={!text.trim()}>
              Ответить
            </Btn>
          </div>
          <div className="mt-2 text-xs text-ink/40">
            Можно ответить и из группового чата HR в MAX — реплаем на карточку обращения.
          </div>
        </>
      ) : (
        <div className="mt-3 text-sm text-ink/50">Обращение решено.</div>
      )}
    </Card>
  )
}
