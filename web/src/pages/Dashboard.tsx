import { useQuery } from '@tanstack/react-query'
import { ArrowRight, BookPlus } from 'lucide-react'
import { Link, useNavigate } from 'react-router'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, fmtDate, type Dashboard as D, type Employee } from '../api'
import {
  FirstReplyStat,
  WaitingTickets,
  firstInLine,
  unanswered,
  useOpenTickets,
} from '../components/ResponseTime'
import SupportToday, { useSupportToday } from '../components/SupportToday'
import { Badge, Btn, Card, Empty, Stat, plural } from '../components/ui'

const DAY = 86_400_000
const WEEKDAY = [
  'в воскресенье',
  'в понедельник',
  'во вторник',
  'в среду',
  'в четверг',
  'в пятницу',
  'в субботу',
]

const pct = (v: number | null) => (v === null ? '—' : `${v}%`)

function greeting() {
  const h = Number(new Date().toLocaleString('ru-RU', { hour: 'numeric', timeZone: 'Europe/Moscow' }))
  return h < 6 ? 'Доброй ночи' : h < 12 ? 'Доброе утро' : h < 18 ? 'Добрый день' : 'Добрый вечер'
}

/** Ближайший выход новичков на неделе вперёд: «1 новичок выходит завтра». Дальше недели день недели неоднозначен. */
function firstDay(emps: Employee[]) {
  const today = new Date().toLocaleDateString('en-CA', { timeZone: 'Europe/Moscow' })
  const dates = emps.filter((e) => e.status !== 'finished' && e.start_date >= today).map((e) => e.start_date)
  const date = dates.sort()[0]
  const days = date ? (Date.parse(date) - Date.parse(today)) / DAY : Infinity
  if (days > 6) return null
  const n = dates.filter((d) => d === date).length
  const when = days === 0 ? 'сегодня' : days === 1 ? 'завтра' : WEEKDAY[new Date(date).getUTCDay()]
  return `${n} ${plural(n, 'новичок выходит', 'новичка выходят', 'новичков выходят')} ${when}`
}

/** Фраза-итог дня и одно главное действие. */
function Summary() {
  const navigate = useNavigate()
  const tickets = useOpenTickets().data
  const support = useSupportToday().data
  const { data: emps } = useQuery({
    queryKey: ['employees'],
    queryFn: () => api.get<Employee[]>('/api/employees'),
  })
  const waiting = (tickets && unanswered(tickets)) ?? []
  const n = waiting.length
  const parts = [
    n > 0 && `${n} ${plural(n, 'обращение ждёт', 'обращения ждут', 'обращений ждут')} ответа`,
    support?.length &&
      `${support.length} ${plural(support.length, 'новичку', 'новичкам', 'новичкам')} нужна поддержка`,
    emps && firstDay(emps),
  ].filter((p): p is string => !!p)
  const loaded = tickets && support && emps

  const today = new Date().toLocaleDateString('ru-RU', {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    timeZone: 'Europe/Moscow',
  })

  let action: { label: string; to: string } | null = null
  if (n)
    action = {
      label: n === 1 ? 'Ответить на обращение' : 'Ответить на обращения',
      to: `/tickets?id=${firstInLine(waiting)!.id}`,
    }
  else if (support?.length)
    action = { label: 'Открыть карточку новичка', to: `/employees/${support[0].employee_id}` }

  return (
    <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-4">
      <div>
        <p className="text-sm text-ink/50 first-letter:uppercase">{today}</p>
        <h1 className="mt-0.5 text-2xl font-semibold tracking-tight">{greeting()}!</h1>
        <p className="mt-2 text-[17px] text-ink/80">
          {parts.length
            ? parts.join(' · ')
            : loaded && 'Всё спокойно: новых обращений и тревожных ответов нет'}
        </p>
      </div>
      {action && (
        <Btn onClick={() => navigate(action.to)}>
          {action.label} <ArrowRight size={16} />
        </Btn>
      )}
    </div>
  )
}

export default function Dashboard() {
  const { data } = useQuery({
    queryKey: ['dashboard'],
    queryFn: () => api.get<D>('/api/dashboard'),
    refetchInterval: 15_000,
  })
  if (!data) return null
  const k = data.kpi

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-6">
      <Summary />

      <WaitingTickets />

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Stat label="Новички на адаптации" value={k.newcomers} />
        <Stat
          label="Корсик ответил сам"
          value={pct(k.ai_resolved_pct)}
          hint={
            k.questions
              ? `из ${k.questions} ${plural(k.questions, 'вопроса', 'вопросов', 'вопросов')} за 30 дней`
              : 'вопросов пока не было'
          }
          tone="lime"
        />
        <Stat label="Ответы были полезны" value={pct(k.helpful_pct)} hint="по оценкам новичков" />
        <Stat
          label="Срочных сигналов"
          value={k.red_flags}
          hint={k.red_flags ? 'нужна реакция сегодня' : 'нет'}
          tone={k.red_flags ? 'red' : undefined}
        />
        <FirstReplyStat />
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <SupportToday />

        <Card title="Настроение новичков" subtitle="Как Корсик оценил свободные ответы — по неделям">
          {data.sentiment_weekly.length === 0 ? (
            <Empty>Данных пока нет — они появятся после первых опросов</Empty>
          ) : (
            <ResponsiveContainer width="100%" height={240}>
              <BarChart
                data={data.sentiment_weekly.map((w) => ({ ...w, week: `с ${fmtDate(w.week)}` }))}
                barSize={28}
              >
                <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#0b2e3a14" />
                <XAxis
                  dataKey="week"
                  tick={{ fontSize: 12, fill: '#0b2e3a99' }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  allowDecimals={false}
                  tick={{ fontSize: 12, fill: '#0b2e3a99' }}
                  axisLine={false}
                  tickLine={false}
                  width={24}
                />
                <Tooltip
                  cursor={{ fill: '#0b2e3a08' }}
                  contentStyle={{ borderRadius: 12, border: '1px solid #0b2e3a1a', fontSize: 13 }}
                />
                <Bar dataKey="positive" name="хорошее" stackId="s" fill="#a6ce39" />
                <Bar dataKey="neutral" name="спокойное" stackId="s" fill="#cbd5e1" />
                <Bar dataKey="negative" name="тревожное" stackId="s" fill="#f43f5e" radius={[6, 6, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </Card>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card title="Где сейчас новички">
          <div className="flex flex-col gap-2.5">
            {data.stages.map((s) => (
              <div key={s.stage} className="flex items-center justify-between text-sm">
                <span className="text-ink/70 first-letter:uppercase">{s.stage}</span>
                <span className="font-semibold tabular-nums">{s.count}</span>
              </div>
            ))}
          </div>
        </Card>
        <Card title="О чём спрашивают">
          {data.question_topics.length === 0 ? (
            <Empty>Вопросов пока не было</Empty>
          ) : (
            <div className="flex flex-col gap-2.5">
              {data.question_topics.map((t) => (
                <div key={t.topic} className="flex items-center justify-between gap-2 text-sm">
                  <span className="text-ink/70 first-letter:uppercase">{t.topic}</span>
                  <Badge>{t.count}</Badge>
                </div>
              ))}
            </div>
          )}
        </Card>
        <Card title="Чего не хватает в базе знаний" subtitle="Корсик не нашёл ответ и передал вопрос HR">
          {data.knowledge_gaps.length === 0 ? (
            <Empty>Корсик ответил на все вопросы</Empty>
          ) : (
            <div className="flex flex-col gap-2">
              {data.knowledge_gaps.slice(0, 5).map((g) => (
                <div key={g.question} className="text-sm text-ink/75 first-letter:uppercase">
                  «{g.question}»
                </div>
              ))}
              <Link
                to="/kb"
                className="mt-2 inline-flex items-center gap-1.5 text-sm font-medium text-sky-700 hover:underline"
              >
                <BookPlus size={15} /> Добавить документ с ответом
              </Link>
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}
