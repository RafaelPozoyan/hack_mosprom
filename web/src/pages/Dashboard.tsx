import { useQuery } from '@tanstack/react-query'
import { ArrowRight, BookPlus, CheckCircle2 } from 'lucide-react'
import { Link, useNavigate } from 'react-router'
import { api, type Dashboard as D, type Employee } from '../api'
import { MoodWeek } from '../components/Mood'
import {
  FirstReplyStat,
  WaitingTickets,
  employeeWords,
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

const muted = (text: string) => <span className="text-lg font-medium text-ink/40">{text}</span>

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

/** Вопросы к Корсику за 30 дней: на что он не нашёл ответа (это можно исправить) и о чём спрашивают чаще. */
function Questions({ d }: { d: D }) {
  const gaps = d.knowledge_gaps.slice(0, 5)
  return (
    <Card
      title="Вопросы новичков"
      subtitle="За 30 дней"
      action={
        gaps.length > 0 && (
          <Link
            to="/kb"
            className="flex items-center gap-1.5 text-sm font-medium text-sky-700 hover:underline"
          >
            <BookPlus size={15} /> Добавить ответ
          </Link>
        )
      }
    >
      {d.kpi.questions === 0 ? (
        <Empty>
          Вопросов пока не было
          <span className="text-xs text-ink/40">
            Когда новички спросят Корсика, здесь появятся их вопросы
          </span>
        </Empty>
      ) : (
        <div className="flex flex-col gap-5">
          {gaps.length ? (
            <div>
              <h3 className="mb-1.5 text-[13px] text-ink/50">Корсик не нашёл ответа</h3>
              {gaps.map((g) => (
                <div key={g.question} className="flex items-baseline justify-between gap-4 py-1 text-sm">
                  <span>«{employeeWords(g.question)}»</span>
                  {g.count > 1 && (
                    <span className="shrink-0 text-xs text-ink/45">
                      спросили {g.count} {plural(g.count, 'раз', 'раза', 'раз')}
                    </span>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <div className="flex items-center gap-2 text-sm text-ink/70">
              <CheckCircle2 size={16} className="text-lime-2" /> Корсик ответил на все вопросы
            </div>
          )}
          {d.question_topics.length > 0 && (
            <div>
              <h3 className="mb-2 text-[13px] text-ink/50">О чём спрашивают</h3>
              <div className="flex flex-wrap gap-2">
                {d.question_topics.map((t) => (
                  <Badge key={t.topic}>
                    <span className="inline-block first-letter:uppercase">{t.topic}</span>
                    <span className="text-ink/45 tabular-nums">{t.count}</span>
                  </Badge>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </Card>
  )
}

export default function Dashboard() {
  const { data, isError, refetch, isFetching } = useQuery({
    queryKey: ['dashboard'],
    queryFn: () => api.get<D>('/api/dashboard'),
    refetchInterval: 15_000,
  })
  const k = data?.kpi
  // Доля в API округлена до процента; при числе вопросов меньше 100 число ответов восстанавливается из неё точно.
  const answered = k?.ai_resolved_pct != null ? Math.round((k.ai_resolved_pct * k.questions) / 100) : 0
  const missed = k ? k.questions - answered : 0
  const stages = data?.stages.filter((s) => s.count > 0) ?? []

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-6">
      <Summary />

      <WaitingTickets />

      {k ? (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          <Stat label="Новички на адаптации" value={k.newcomers} />
          <Stat
            label="Корсик ответил сам"
            value={
              !k.questions
                ? muted('вопросов пока не было')
                : k.questions < 100
                  ? `${answered} из ${k.questions}`
                  : `${k.ai_resolved_pct}%`
            }
            hint={
              !k.questions
                ? undefined
                : k.questions < 100
                  ? `${missed ? `на ${missed} не нашёл ответа · ` : ''}за 30 дней`
                  : `из ${k.questions} ${plural(k.questions, 'вопроса', 'вопросов', 'вопросов')} за 30 дней`
            }
            tone="lime"
          />
          <Stat
            label="Ответы были полезны"
            value={k.helpful_pct == null ? muted('оценок пока нет') : `${k.helpful_pct}%`}
            hint={k.helpful_pct == null ? undefined : 'по оценкам новичков'}
          />
          <FirstReplyStat />
        </div>
      ) : (
        isError && (
          <Card>
            <Empty>
              Не удалось загрузить показатели. Проверьте соединение.
              <Btn small variant="ghost" onClick={() => refetch()} disabled={isFetching}>
                Повторить
              </Btn>
            </Empty>
          </Card>
        )
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <SupportToday />
        {data && <Questions d={data} />}
      </div>

      {data && (
        <Card title="Настроение новичков" subtitle="По ответам новичков в опросах и чате">
          <MoodWeek weeks={data.sentiment_weekly} />
        </Card>
      )}

      {data && (
        <Card>
          <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2">
            <h2 className="text-[15px] font-semibold">Где сейчас новички</h2>
            {stages.length ? (
              <div className="flex flex-wrap gap-x-5 gap-y-1 text-sm text-ink/70">
                {stages.map((s) => (
                  <span key={s.stage}>
                    <span className="inline-block first-letter:uppercase">{s.stage}</span>{' '}
                    <span className="font-semibold text-ink tabular-nums">{s.count}</span>
                  </span>
                ))}
              </div>
            ) : (
              <span className="text-sm text-ink/50">Новичков пока нет</span>
            )}
          </div>
        </Card>
      )}
    </div>
  )
}
