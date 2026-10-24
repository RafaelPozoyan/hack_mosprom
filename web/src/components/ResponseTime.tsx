import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { AlarmClock, BellRing, ChevronDown } from 'lucide-react'
import { Link } from 'react-router'
import { ApiError, api, fmtDateTime, type ResponseTime, type Ticket } from '../api'
import { Avatar, Badge, Btn, Card, Stat, TICKET_KIND, plural } from './ui'

const useResponseTime = () =>
  useQuery({
    queryKey: ['response-time'],
    queryFn: () => api.get<ResponseTime>('/api/response-time'),
    refetchInterval: 15_000,
  })

/** Длительность для людей. Рабочие часы не переводим в дни: «1 день» читался бы как сутки. */
function duration(min: number, workHours = false) {
  const total = Math.round(min)
  if (total < 1) return '< 1 мин'
  if (total < 60) return `${total} мин`
  const h = Math.floor(total / 60)
  const m = total % 60
  if (!workHours && h >= 24) {
    const d = Math.floor(h / 24)
    return `${d} ${plural(d, 'день', 'дня', 'дней')}`
  }
  return m ? `${h} ч ${m} мин` : `${h} ч`
}

const waited = (iso: string) => duration((Date.now() - new Date(iso).getTime()) / 60000)
const workHours = (n: number) => `${n} ${plural(n, 'рабочий час', 'рабочих часа', 'рабочих часов')}`
const responsible = (recruiter: string | null) =>
  recruiter ? `Ответственный: ${recruiter}` : 'Рекрутер не назначен'

/** «Сегодня»: сколько обычно новичок ждёт первого ответа HR; как посчитано — в подсказке. */
export function FirstReplyStat() {
  const { data, isError } = useResponseTime()
  const f = data?.first_reply
  let hint = ''
  if (isError) hint = 'не удалось загрузить'
  else if (f) hint = f.tickets ? 'обычно, в рабочие часы' : 'ответов пока не было'
  const details = f?.tickets
    ? `Медиана: половина первых ответов HR — быстрее. ${f.tickets} ${plural(f.tickets, 'обращение', 'обращения', 'обращений')} за ${f.days} дней; ночь, выходные и праздники не считаются.`
    : undefined
  return (
    <Stat
      label="Первый ответ HR"
      value={f?.median_min != null ? duration(f.median_min, true) : '—'}
      hint={hint}
      details={details}
    />
  )
}

/** «Сегодня»: обращения без ответа HR дольше срока. Пока таких нет, блок не занимает места. */
export function OverdueTickets() {
  const { data, isError } = useResponseTime()
  if (isError) {
    return (
      <Card title="Просрочено">
        <div role="alert" className="text-sm text-rose-600">
          Не удалось проверить сроки ответа. Обновите страницу через минуту.
        </div>
      </Card>
    )
  }
  if (!data?.overdue.length) return null
  const { hours, urgent_hours } = data.rules
  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <AlarmClock size={17} className="text-rose-600" /> Просрочено: {data.overdue.length}
        </span>
      }
      subtitle="Новички ждут ответа HR дольше срока"
    >
      <div className="flex flex-col divide-y divide-ink/[0.06]">
        {data.overdue.map((t) => (
          <OverdueRow key={t.id} t={t} />
        ))}
      </div>
      <details className="group mt-2 text-[13px] text-ink/55">
        <summary className="flex w-fit cursor-pointer list-none items-center gap-1 select-none hover:text-ink/80">
          Как считается срок <ChevronDown size={14} className="transition group-open:rotate-180" />
        </summary>
        <p className="mt-1.5 max-w-3xl">
          Отвечает рекрутер новичка. На первый ответ — {workHours(hours)}, на срочное обращение —{' '}
          {workHours(urgent_hours)}; ночь, выходные и праздники не считаются. Когда срок выходит, рекрутер
          получает напоминание, а HR-руководитель — уведомление в MAX.
        </p>
      </details>
    </Card>
  )
}

function OverdueRow({ t }: { t: ResponseTime['overdue'][number] }) {
  const qc = useQueryClient()
  const r = t.response
  const remind = useMutation({
    mutationFn: () => api.post<{ delivered: boolean }>(`/api/tickets/${t.id}/remind`),
    onSettled: () => qc.invalidateQueries({ queryKey: ['response-time'] }),
  })
  let note = null
  if (remind.isError) {
    const reason = remind.error instanceof ApiError ? remind.error.detail : null
    note = (
      <div role="alert" className="text-rose-600">
        {reason ?? 'Не получилось напомнить. Попробуйте ещё раз.'}
      </div>
    )
  } else if (r.reminded_at) {
    note = r.reminder_delivered ? (
      <div className="text-emerald-700">Напомнили {fmtDateTime(r.reminded_at)}</div>
    ) : (
      <div className="text-amber-800">Напоминание не дошло в MAX — свяжитесь с рекрутером лично</div>
    )
  }
  return (
    <div className="flex items-center gap-3 py-3">
      <Avatar name={t.employee} size={36} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <Link to={`/tickets?id=${t.id}`} className="font-medium hover:underline">
            {t.employee}
          </Link>
          <Badge tone={TICKET_KIND[t.kind]?.tone}>{TICKET_KIND[t.kind]?.label}</Badge>
        </div>
        <div className="text-[13px] text-ink/55">
          {responsible(r.recruiter)} · без ответа {waited(t.created_at)}
        </div>
        {note && <div className="mt-0.5 text-xs">{note}</div>}
      </div>
      <Btn small variant="ghost" onClick={() => remind.mutate()} disabled={remind.isPending || !r.recruiter}>
        <BellRing size={14} /> {remind.isPending ? 'Отправляю…' : 'Напомнить рекрутеру'}
      </Btn>
    </div>
  )
}

/** Обращение: кто отвечает и сколько новичок ждёт первого ответа HR. */
export function ResponseLine({ t }: { t: Ticket }) {
  const r = t.response
  if (!r) return null
  let state = ''
  if (r.first_reply_at) {
    state = new Date(r.first_reply_at) > new Date(r.due_at) ? 'ответили позже срока' : 'ответили в срок'
  } else if (t.status === 'open') {
    state = `ждёт ответа ${waited(t.created_at)} · ${r.overdue ? 'срок вышел' : `ответить до ${fmtDateTime(r.due_at)}`}`
  }
  return (
    <div
      className={clsx(
        'mb-3 flex items-center gap-1.5 text-[13px]',
        r.overdue ? 'text-rose-700' : 'text-ink/60',
      )}
    >
      <AlarmClock size={15} className="shrink-0" />
      {responsible(r.recruiter)}
      {state && ` · ${state}`}
    </div>
  )
}
