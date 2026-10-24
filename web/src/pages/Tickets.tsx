import { useQuery, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { CheckCircle2, Sparkles } from 'lucide-react'
import { Fragment, useEffect, useRef, useState } from 'react'
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
      <div className={clsx('grid gap-5', selected && 'lg:grid-cols-[360px_1fr]')}>
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
              {status === 'open' ? 'Все обращения разобраны' : 'Здесь будут закрытые обращения'}
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
                    </div>
                    <div className="mt-1 line-clamp-2 text-xs text-ink/55">{t.subject}</div>
                  </div>
                </button>
              ))}
            </div>
          )}
        </Card>
        {selected && <TicketView key={selected} id={selected} />}
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
  const box = useRef<HTMLDivElement>(null)
  const start = useRef<HTMLDivElement>(null)
  // Переписка открывается на начале обращения: всё, что выше, — предыстория для справки.
  useEffect(() => {
    if (box.current && start.current) box.current.scrollTop = start.current.offsetTop - 4
  }, [t?.id])
  if (!t) return null
  // Сервер отдаёт до 8 сообщений до обращения и все после; граница — первое сообщение не раньше обращения.
  const created = new Date(t.created_at).getTime()
  const first = t.messages?.findIndex((m) => new Date(m.created_at).getTime() >= created) ?? -1
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
      <div
        ref={box}
        className="relative flex max-h-[440px] flex-col gap-2 overflow-y-auto rounded-2xl bg-ink/[0.03] p-3"
      >
        {t.messages?.map((m, i) => (
          <Fragment key={m.id}>
            {i === first && i > 0 && (
              <div ref={start} className="flex items-center gap-3 py-1 text-[11px] font-medium text-ink/45">
                <span className="h-px flex-1 bg-ink/10" />
                обращение {fmtDateTime(t.created_at)}
                <span className="h-px flex-1 bg-ink/10" />
              </div>
            )}
            <div
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
          </Fragment>
        ))}
      </div>
      {t.status === 'open' ? (
        <>
          <div className="mt-3 flex flex-wrap gap-2">
            <input
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && reply()}
              placeholder="Напишите ответ — он придёт новичку в чат с Корсиком"
              className={clsx(inputCls, 'min-w-60 flex-1')}
            />
            <Btn onClick={reply} disabled={!text.trim()}>
              Ответить
            </Btn>
            <Btn variant="ghost" onClick={close}>
              {t.response?.first_reply_at ? 'Закрыть обращение' : 'Закрыть без ответа'}
            </Btn>
          </div>
          <div className="mt-2 text-xs text-ink/40">
            Можно ответить и в MAX: в чате HR ответьте на сообщение с этим обращением.
          </div>
        </>
      ) : (
        <div className="mt-3 text-sm text-ink/50">Обращение решено.</div>
      )}
    </Card>
  )
}
