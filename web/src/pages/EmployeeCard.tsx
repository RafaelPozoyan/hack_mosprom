import { useQuery, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { BellRing, CheckCircle2, Clock, Hand, MinusCircle, Play, QrCode, Upload } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router'
import {
  ApiError,
  PHASES,
  api,
  fmtDateTime,
  probationDay,
  type EmployeeCardData,
  type TimelineStep,
} from '../api'
import ChatPhone from '../components/ChatPhone'
import {
  Avatar,
  Badge,
  Btn,
  Card,
  Empty,
  Progress,
  SENTIMENT,
  SEVERITY,
  TICKET_KIND,
  Tabs,
  siteName,
  timeAgo,
  useDemo,
} from '../components/ui'
import { InviteModal } from './Employees'

type Tab = 'journey' | 'surveys' | 'signals' | 'tickets'

export default function EmployeeCard() {
  const { id } = useParams()
  const qc = useQueryClient()
  const demo = useDemo()
  const [tab, setTab] = useState<Tab>('journey')
  const [invite, setInvite] = useState(false)
  const { data } = useQuery({
    queryKey: ['employee', id],
    queryFn: () => api.get<EmployeeCardData>(`/api/employees/${id}`),
    refetchInterval: 10_000,
  })
  const refresh = useCallback(() => qc.invalidateQueries({ queryKey: ['employee', id] }), [qc, id])
  if (!data) return null
  const e = data.employee
  const { day, total } = probationDay(e)
  const concerns =
    data.analyses.filter((a) => a.severity !== 'low').length +
    data.surveys.flatMap((g) => g.items).filter((i) => i.flag).length
  const openTickets = data.tickets.filter((t) => t.status === 'open').length

  return (
    <div className={clsx('mx-auto flex gap-6', demo ? 'max-w-[1500px]' : 'max-w-5xl')}>
      <div className="flex min-w-0 flex-1 flex-col gap-5">
        <Card>
          <div className="flex flex-wrap items-center gap-4">
            <Avatar name={e.full_name} size={56} />
            <div className="min-w-0 flex-1">
              <h1 className="text-xl font-semibold tracking-tight">{e.full_name}</h1>
              <div className="text-sm text-ink/55">
                {e.position || 'Должность не указана'} · {siteName(e.site_code)}
              </div>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {e.address_form && <Badge>общается на «{e.address_form === 'vy' ? 'вы' : 'ты'}»</Badge>}
                {e.in_dialog_with_hr && <Badge tone="blue">сейчас в диалоге с HR</Badge>}
                {e.manager && <Badge>руководитель: {e.manager}</Badge>}
                {e.recruiter && <Badge>рекрутер: {e.recruiter}</Badge>}
              </div>
            </div>
            <Btn variant="ghost" onClick={() => setInvite(true)}>
              <QrCode size={16} /> QR-приглашение
            </Btn>
          </div>
          <div className="mt-5">
            {e.status === 'invited' ? (
              <div className="rounded-xl bg-sky-50 px-4 py-3 text-sm text-sky-900">
                Ждём, когда {e.first_name} отсканирует QR-код из оффера и запустит Корсика в MAX.
              </div>
            ) : day < 1 ? (
              <div className="text-sm text-ink/60">
                До выхода на работу: {1 - day} дн. Корсик уже на связи и помогает подготовиться.
              </div>
            ) : (
              <>
                <div className="mb-1.5 flex justify-between text-sm">
                  <span className="text-ink/60">Испытательный срок</span>
                  <span className="font-medium tabular-nums">
                    день {Math.min(day, total)} из {total}
                  </span>
                </div>
                <Progress value={(100 * day) / total} />
              </>
            )}
          </div>
        </Card>

        <Tabs
          value={tab}
          onChange={setTab}
          items={[
            { id: 'journey', label: 'Путь адаптации' },
            { id: 'surveys', label: 'Опросы' },
            { id: 'signals', label: 'Сигналы', count: concerns },
            { id: 'tickets', label: 'Обращения', count: openTickets },
          ]}
        />

        {tab === 'journey' && <Journey steps={data.timeline} empId={e.id} onDone={refresh} />}
        {tab === 'surveys' && <Surveys data={data} />}
        {tab === 'signals' && <Signals data={data} />}
        {tab === 'tickets' && <TicketsTab data={data} />}
      </div>

      {demo && (
        <div className="hidden shrink-0 xl:block">
          <div className="sticky top-6">
            <div className="mb-2 text-center text-xs font-medium text-sky-800">
              Так {e.first_name} видит Корсика в MAX
            </div>
            <ChatPhone employee={e} onActivity={refresh} />
          </div>
        </div>
      )}
      {invite && <InviteModal e={e} onClose={() => setInvite(false)} />}
    </div>
  )
}

// --- путь адаптации ----------------------------------------------------------------------------------

const STATUS: Record<string, { label: string; icon: typeof Clock; cls: string }> = {
  sent: { label: 'отправлено', icon: CheckCircle2, cls: 'text-lime-2' },
  done: { label: 'отправлено', icon: CheckCircle2, cls: 'text-lime-2' },
  pending: { label: 'запланировано', icon: Clock, cls: 'text-ink/35' },
  running: { label: 'отправляется', icon: Clock, cls: 'text-sky' },
  waiting: { label: 'ждём обязательный ответ', icon: Clock, cls: 'text-amber-600' },
  await_start: { label: 'ждём запуска бота по QR', icon: Clock, cls: 'text-amber-600' },
  blocked: { label: 'после обязательного ответа', icon: Clock, cls: 'text-amber-600' },
  manual: { label: 'по вашей команде', icon: Hand, cls: 'text-amber-600' },
  failed: { label: 'не удалось отправить', icon: MinusCircle, cls: 'text-rose-500' },
  skipped: { label: 'пропущено', icon: MinusCircle, cls: 'text-ink/25' },
  cancelled: { label: 'пропущено', icon: MinusCircle, cls: 'text-ink/25' },
}

function Journey({ steps, empId, onDone }: { steps: TimelineStep[]; empId: number; onDone: () => void }) {
  const groups = PHASES.map((p) => ({ phase: p, items: steps.filter((s) => s.phase === p) })).filter(
    (g) => g.items.length,
  )
  const waiting = steps.find((s) => s.status === 'waiting')
  return (
    <div className="flex flex-col gap-4">
      {waiting && (
        <Card title="Ждём обязательный ответ">
          <p className="text-sm text-ink/70">
            На шаг «{waiting.title}» пока нет полного ответа. Следующие опросы продолжатся после него.
            Справочная информация и помощь HR остаются доступны.
          </p>
        </Card>
      )}
      {groups.map((g) => (
        <Card key={g.phase} title={g.phase}>
          <ol className="flex flex-col">
            {g.items.map((t) => (
              <Step key={t.step_id} t={t} empId={empId} onDone={onDone} />
            ))}
          </ol>
        </Card>
      ))}
    </div>
  )
}

function Step({ t, empId, onDone }: { t: TimelineStep; empId: number; onDone: () => void }) {
  const demo = useDemo()
  const file = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    setError('')
  }, [t.status])
  const st = STATUS[t.status] ?? STATUS.pending
  const Icon = st.icon
  const run = async (f?: File) => {
    setBusy(true)
    const fd = new FormData()
    if (f) fd.append('file', f)
    setError('')
    try {
      await api.post(`/api/employees/${empId}/steps/${t.step_id}/run`, fd)
      onDone()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setError(e.detail ?? 'Шаг сейчас недоступен. Обновите карточку и проверьте его состояние.')
        onDone()
      } else if (e instanceof ApiError && e.status === 503) {
        setError('Отправка не завершена. Корсик повторит её автоматически. Обновите карточку позже.')
      } else {
        setError('Не удалось подтвердить отправку. Обновите карточку и проверьте сообщения перед повтором.')
      }
    } finally {
      setBusy(false)
      if (file.current) file.current.value = ''
    }
  }
  const sent = t.status === 'sent' || t.status === 'done'
  const obsolete = t.status === 'skipped' && Boolean(t.status_note)
  return (
    <li className="flex items-start gap-3 border-b border-ink/[0.05] py-2.5 last:border-0">
      <Icon size={18} className={clsx('mt-0.5 shrink-0', st.cls)} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className={clsx('font-medium', sent && 'text-ink/60')}>{t.title}</span>
          {t.audience === 'manager' && <Badge tone="blue">руководителю</Badge>}
          {t.audience === 'recruiter' && <Badge tone="blue">рекрутеру</Badge>}
        </div>
        <div className="mt-0.5 line-clamp-1 text-xs text-ink/45">{t.preview}</div>
        {t.run_at && ['waiting', 'blocked', 'await_start'].includes(t.status) && (
          <div className="mt-1 text-xs text-amber-700">{st.label}</div>
        )}
        {t.status_note && <div className="mt-1 text-xs text-ink/60">{t.status_note}</div>}
        {error && (
          <div role="alert" className="mt-2 text-sm text-rose-600">
            {error}
          </div>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <span className="text-xs text-ink/45">{t.run_at ? fmtDateTime(t.run_at) : st.label}</span>
        {t.manual && !sent && !obsolete && (
          <>
            {t.step_id === 'd3_plan' && (
              <>
                <input
                  ref={file}
                  type="file"
                  hidden
                  onChange={(e) => e.target.files?.[0] && run(e.target.files[0])}
                />
                <Btn small variant="ghost" onClick={() => file.current?.click()} disabled={busy}>
                  <Upload size={13} /> Приложить план
                </Btn>
              </>
            )}
            <Btn small onClick={() => run()} disabled={busy}>
              Отправить
            </Btn>
          </>
        )}
        {demo && !t.manual && !sent && !obsolete && (
          <Btn small demo onClick={() => run()} disabled={busy}>
            <Play size={12} /> {t.status === 'waiting' ? 'Продолжить' : 'сейчас'}
          </Btn>
        )}
      </div>
    </li>
  )
}

// --- опросы, сигналы, обращения ----------------------------------------------------------------------

function Surveys({ data }: { data: EmployeeCardData }) {
  if (!data.surveys.length)
    return (
      <Card>
        <Empty>Опросов пока не было. Первый — по итогам третьего рабочего дня.</Empty>
      </Card>
    )
  return (
    <div className="flex flex-col gap-4">
      {data.surveys.map((g) => (
        <Card key={g.title} title={g.title} subtitle={timeAgo(g.at)}>
          <div className="flex flex-col divide-y divide-ink/[0.05]">
            {g.items.map((i, n) => (
              <div key={n} className="py-2.5">
                <div className="flex items-start justify-between gap-4 text-sm">
                  <span className="text-ink/70">{i.question}</span>
                  <span className={clsx('shrink-0 text-right font-medium', i.concern && 'text-rose-600')}>
                    {i.answer}
                  </span>
                </div>
                {i.comment && (
                  <div className="mt-1.5 rounded-lg bg-ink/[0.03] px-3 py-2 text-sm text-ink/75">
                    «{i.comment}»
                  </div>
                )}
              </div>
            ))}
          </div>
        </Card>
      ))}
    </div>
  )
}

function Signals({ data }: { data: EmployeeCardData }) {
  const items = [...data.analyses].sort(
    (a, b) => Number(b.severity === 'high') - Number(a.severity === 'high'),
  )
  return (
    <div className="flex flex-col gap-4">
      <Card
        title="Что заметил Корсик"
        subtitle="Корсик читает свободные ответы и сообщения новичка и выделяет, что его беспокоит"
      >
        {items.length === 0 ? (
          <Empty>Пока нечего анализировать</Empty>
        ) : (
          <div className="flex flex-col gap-3">
            {items.map((a, i) => (
              <div
                key={i}
                className={clsx(
                  'rounded-2xl border p-4',
                  a.severity === 'high' ? 'border-rose-200 bg-rose-50/40' : 'border-ink/[0.07]',
                )}
              >
                <div className="text-sm">«{a.text}»</div>
                <div className="mt-2.5 flex flex-wrap items-center gap-1.5 text-xs">
                  <span className="text-ink/45">Настроение:</span>
                  <Badge tone={SENTIMENT[a.sentiment]?.tone}>{SENTIMENT[a.sentiment]?.label}</Badge>
                  {a.risk_flags.map((f) => (
                    <Badge key={f} tone="red">
                      {f}
                    </Badge>
                  ))}
                  {a.topics
                    .filter((t) => t !== 'другое')
                    .map((t) => (
                      <Badge key={t}>{t}</Badge>
                    ))}
                </div>
                {a.summary && <div className="mt-2 text-sm text-ink/60">{a.summary}</div>}
                <div className="mt-2 flex justify-between text-xs text-ink/40">
                  <span>{SEVERITY[a.severity]?.label}</span>
                  <span>
                    {a.source === 'survey' ? 'из опроса' : 'из сообщения'} · {timeAgo(a.at)}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
      <Card title="Кого Корсик уведомил">
        {data.notifications.length === 0 ? (
          <Empty>Уведомлений не было</Empty>
        ) : (
          <div className="flex flex-col gap-2">
            {data.notifications.map((n, i) => (
              <div key={i} className="flex gap-3 rounded-xl bg-sky-50/60 p-3 text-sm">
                <BellRing size={16} className="mt-0.5 shrink-0 text-sky-700" />
                <div className="min-w-0 flex-1">
                  <div className="mb-0.5 text-xs text-ink/50">
                    {n.to === 'manager' ? 'Руководителю' : 'Рекрутеру'} · {timeAgo(n.at)} ·{' '}
                    {n.delivered ? 'в MAX' : 'в панели'}
                  </div>
                  <div className="whitespace-pre-wrap text-ink/80">{n.text}</div>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}

function TicketsTab({ data }: { data: EmployeeCardData }) {
  if (!data.tickets.length)
    return (
      <Card>
        <Empty>Обращений не было</Empty>
      </Card>
    )
  return (
    <Card>
      <div className="flex flex-col divide-y divide-ink/[0.05]">
        {data.tickets.map((t) => (
          <Link
            key={t.id}
            to={`/tickets?id=${t.id}`}
            className="-mx-2 flex items-center gap-3 rounded-xl px-2 py-3 hover:bg-ink/[0.025]"
          >
            <Badge tone={TICKET_KIND[t.kind]?.tone}>{TICKET_KIND[t.kind]?.label}</Badge>
            <span className="min-w-0 flex-1 truncate text-sm">{t.subject}</span>
            <span className="text-xs text-ink/45">
              {t.status === 'open' ? 'открыто' : 'решено'} · {timeAgo(t.created_at)}
            </span>
          </Link>
        ))}
      </div>
    </Card>
  )
}
