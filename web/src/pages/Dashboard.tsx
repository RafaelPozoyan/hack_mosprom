import { useQuery } from '@tanstack/react-query'
import { BookPlus } from 'lucide-react'
import { Link } from 'react-router'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, fmtDate, type Dashboard as D } from '../api'
import { FirstReplyStat, WaitingTickets } from '../components/ResponseTime'
import SupportToday from '../components/SupportToday'
import { Badge, Card, Empty, Stat, plural } from '../components/ui'

const pct = (v: number | null) => (v === null ? '—' : `${v}%`)

function greeting() {
  const h = Number(new Date().toLocaleString('ru-RU', { hour: 'numeric', timeZone: 'Europe/Moscow' }))
  return h < 6 ? 'Доброй ночи' : h < 12 ? 'Доброе утро' : h < 18 ? 'Добрый день' : 'Добрый вечер'
}

export default function Dashboard() {
  const { data } = useQuery({
    queryKey: ['dashboard'],
    queryFn: () => api.get<D>('/api/dashboard'),
    refetchInterval: 15_000,
  })
  if (!data) return null
  const k = data.kpi
  const today = new Date().toLocaleDateString('ru-RU', {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    timeZone: 'Europe/Moscow',
  })

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{greeting()}!</h1>
        <p className="mt-1 text-sm text-ink/50 first-letter:uppercase">{today}</p>
      </div>

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
