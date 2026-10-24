import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { AlarmClock, ArrowRight, BellRing, CheckCircle2, ChevronDown } from 'lucide-react'
import { Link } from 'react-router'
import { ApiError, api, fmtDate, fmtDateTime, fmtTime, type ResponseTime, type Ticket } from '../api'
import { Avatar, Btn, Card, Empty, Stat, plural, timeAgo } from './ui'

const useResponseTime = () =>
  useQuery({
    queryKey: ['response-time'],
    queryFn: () => api.get<ResponseTime>('/api/response-time'),
    refetchInterval: 15_000,
  })

/** Открытые обращения — тот же запрос, что у счётчика «Обращения» в меню (App.tsx). */
export const useOpenTickets = () =>
  useQuery({
    queryKey: ['tickets', 'open'],
    queryFn: () => api.get<Ticket[]>('/api/tickets?status=open'),
    refetchInterval: 10_000,
  })

/** Слова новичка с заглавной буквы. Тема обращения из опроса — «вопрос → ответ» (bot/surveys.py): берём ответ. */
export function employeeWords(text: string) {
  const words = text.split(' → ').at(-1)!.trim()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

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
const until = (iso: string) =>
  fmtDate(iso) === fmtDate(new Date().toISOString()) ? fmtTime(iso) : fmtDateTime(iso)

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

type Person = {
  id: number
  name: string
  tickets: Ticket[] // от старого к новому
  main: Ticket // о нём строка: самое давнее с вышедшим сроком, иначе срочное, иначе самое давнее
  overdue: boolean
  urgent: boolean // срочное обращение: срок ответа короче, ждать до общего срока нельзя
}

const rank = (p: Person) => (p.overdue ? 0 : p.urgent ? 1 : 2)

/** Обращения без ответа HR — то, что ждёт ответа; где HR уже ответил, разговор идёт в «Обращениях». */
export const unanswered = (tickets: Ticket[]) => tickets.filter((t) => !t.response?.first_reply_at)

/** Одна строка на новичка: сначала у кого срок вышел, затем срочные; внутри — кто дольше ждёт. */
function byEmployee(tickets: Ticket[]): Person[] {
  const groups = new Map<number, Ticket[]>()
  for (const t of tickets) groups.set(t.employee_id, [...(groups.get(t.employee_id) ?? []), t])
  return [...groups.values()]
    .map((list) => {
      const sorted = list.toSorted((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at))
      const overdue = sorted.find((t) => t.response?.overdue)
      const urgent = sorted.find((t) => t.severity === 'high')
      return {
        id: sorted[0].employee_id,
        name: sorted[0].employee,
        tickets: sorted,
        main: overdue ?? urgent ?? sorted[0],
        overdue: !!overdue,
        urgent: !!urgent,
      }
    })
    .sort((a, b) => rank(a) - rank(b) || Date.parse(a.main.created_at) - Date.parse(b.main.created_at))
}

/** Обращение, с которого стоит начать: первая строка «Ждут вашего ответа». */
export const firstInLine = (tickets: Ticket[]): Ticket | undefined => byEmployee(tickets)[0]?.main

/** «Сегодня»: обращения без ответа HR по новичкам. */
export function WaitingTickets() {
  const { data, isError, refetch, isFetching } = useOpenTickets()
  const rules = useResponseTime().data?.rules
  if (isError && !data) {
    return (
      <Card title="Ждут вашего ответа">
        <Empty>
          Не удалось загрузить обращения. Проверьте соединение.
          <Btn small variant="ghost" onClick={() => refetch()} disabled={isFetching}>
            Повторить
          </Btn>
        </Empty>
      </Card>
    )
  }
  if (!data) return null
  const people = byEmployee(unanswered(data))
  return (
    <Card
      title="Ждут вашего ответа"
      subtitle={people.length ? 'Корсик передал вам' : undefined}
      action={
        data.length > 0 && (
          <Link
            to="/tickets"
            className="flex items-center gap-1 text-sm font-medium text-sky-700 hover:underline"
          >
            Все обращения <ArrowRight size={15} />
          </Link>
        )
      }
    >
      {people.length === 0 ? (
        <Empty icon={<CheckCircle2 className="text-lime-2" />}>
          {data.length ? 'Вы ответили на все обращения' : 'Новых обращений нет'}
          <span className="text-xs text-ink/40">Новое появится здесь, когда новичок попросит помощи HR</span>
        </Empty>
      ) : (
        <div className="flex flex-col divide-y divide-ink/[0.06]">
          {people.map((p) => (
            <PersonRow key={p.id} p={p} />
          ))}
        </div>
      )}
      {rules && people.length > 0 && (
        <details className="group mt-2 text-[13px] text-ink/55">
          <summary className="flex w-fit cursor-pointer list-none items-center gap-1 select-none hover:text-ink/80">
            Как считается срок <ChevronDown size={14} className="transition group-open:rotate-180" />
          </summary>
          <p className="mt-1.5 max-w-3xl">
            Отвечает рекрутер новичка. На первый ответ — {workHours(rules.hours)}, на срочное обращение —{' '}
            {workHours(rules.urgent_hours)}; ночь, выходные и праздники не считаются. Когда срок выходит,
            рекрутер получает напоминание, а HR-руководитель — уведомление в MAX.
          </p>
        </details>
      )}
    </Card>
  )
}

function PersonRow({ p }: { p: Person }) {
  const qc = useQueryClient()
  const t = p.main
  const late = p.overdue ? t.response : undefined
  const remind = useMutation({
    mutationFn: (id: number) => api.post<{ delivered: boolean }>(`/api/tickets/${id}/remind`),
    onSettled: () => qc.invalidateQueries({ queryKey: ['tickets'] }),
  })
  const count = p.tickets.length
  const about = [
    count > 1 &&
      `${count} ${plural(count, 'обращение', 'обращения', 'обращений')}, последнее ${timeAgo(p.tickets.at(-1)!.created_at)}`,
    late && (late.recruiter ? `отвечает ${late.recruiter}` : 'рекрутер не назначен'),
  ].filter(Boolean)
  let state = ''
  if (p.overdue) state = `ждёт ответа ${waited(t.created_at)}`
  else if (t.response) state = `${p.urgent ? 'срочно · ' : ''}ответить до ${until(t.response.due_at)}`
  let note = null
  if (remind.isError) {
    const reason = remind.error instanceof ApiError ? remind.error.detail : null
    note = (
      <div role="alert" className="text-rose-600">
        {reason ?? 'Не получилось напомнить. Попробуйте ещё раз.'}
      </div>
    )
  } else if (late?.reminded_at) {
    note = late.reminder_delivered ? (
      <div className="text-emerald-700">Напомнили {fmtDateTime(late.reminded_at)}</div>
    ) : (
      <div className="text-amber-800">Напоминание не дошло в MAX — свяжитесь с рекрутером лично</div>
    )
  }
  return (
    <div className="flex items-center gap-4 py-2">
      <Link
        to={`/tickets?id=${t.id}`}
        className="-mx-2 flex min-w-0 flex-1 items-center gap-3 rounded-xl px-2 py-2 transition hover:bg-ink/[0.025]"
      >
        <Avatar name={p.name} size={36} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="font-medium">{p.name}</span>
            {about.length > 0 && <span className="text-[13px] text-ink/50">{about.join(' · ')}</span>}
          </div>
          <div className="truncate text-sm text-ink/65">{employeeWords(t.subject)}</div>
          {note && <div className="mt-0.5 text-xs">{note}</div>}
        </div>
      </Link>
      <span
        className={clsx(
          'shrink-0 text-[13px]',
          p.overdue || p.urgent ? 'font-medium text-rose-700' : 'text-ink/50',
        )}
      >
        {state}
      </span>
      {p.overdue && (
        <Btn
          small
          variant="ghost"
          onClick={() => remind.mutate(t.id)}
          disabled={remind.isPending || !late?.recruiter}
        >
          <BellRing size={14} /> {remind.isPending ? 'Отправляю…' : 'Напомнить рекрутеру'}
        </Btn>
      )}
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
