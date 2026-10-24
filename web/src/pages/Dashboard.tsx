import { useQuery } from '@tanstack/react-query'
import { ArrowRight, BookPlus, CheckCircle2, HeartHandshake } from 'lucide-react'
import { Link } from 'react-router'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, fmtDate, type Dashboard as D } from '../api'
import { FirstReplyStat, OverdueTickets } from '../components/ResponseTime'
import { Avatar, Badge, Card, Empty, Stat, TICKET_KIND, plural, siteName, timeAgo } from '../components/ui'

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

      <OverdueTickets />

      <Card
        title="Требует внимания"
        subtitle={data.attention.length ? 'Обращения новичков, которые Корсик передал вам' : undefined}
        action={
          data.attention.length > 0 && (
            <Link
              to="/tickets"
              className="flex items-center gap-1 text-sm font-medium text-sky-700 hover:underline"
            >
              Все обращения <ArrowRight size={15} />
            </Link>
          )
        }
      >
        {data.attention.length === 0 ? (
          <Empty icon={<CheckCircle2 className="text-lime-2" />}>Всё спокойно — новых обращений нет</Empty>
        ) : (
          <div className="flex flex-col divide-y divide-ink/[0.06]">
            {data.attention.map((t) => (
              <Link
                key={t.id}
                to={`/tickets?id=${t.id}`}
                className="-mx-2 flex items-center gap-3 rounded-xl px-2 py-3 transition hover:bg-ink/[0.025]"
              >
                <Avatar name={t.employee} size={36} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-medium">{t.employee}</span>
                    <Badge tone={TICKET_KIND[t.kind]?.tone}>{TICKET_KIND[t.kind]?.label}</Badge>
                  </div>
                  <div className="truncate text-sm text-ink/60">{t.subject}</div>
                </div>
                <span className="shrink-0 text-xs text-ink/40">{timeAgo(t.created_at)}</span>
              </Link>
            ))}
          </div>
        )}
      </Card>

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
        <Card title="Кому нужна поддержка" subtitle="Сигналы из опросов и сообщений за 30 дней">
          {data.risk.length === 0 ? (
            <Empty icon={<HeartHandshake className="text-ink/30" />}>
              Сигналов нет — новички чувствуют себя хорошо
            </Empty>
          ) : (
            <div className="flex flex-col divide-y divide-ink/[0.06]">
              {data.risk.slice(0, 6).map((r) => (
                <Link
                  key={r.employee_id}
                  to={`/employees/${r.employee_id}`}
                  className="-mx-2 flex items-start gap-3 rounded-xl px-2 py-3 hover:bg-ink/[0.025]"
                >
                  <Avatar name={r.name} size={36} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 font-medium">
                      {r.name}
                      {r.high && <Badge tone="red">срочно</Badge>}
                    </div>
                    <div className="text-xs text-ink/45">
                      {siteName(r.site)} · {r.stage}
                    </div>
                    <div className="mt-1.5 flex flex-wrap gap-1">
                      {[...new Set(r.signals.map((s) => s.flag))].slice(0, 4).map((f) => (
                        <Badge key={f} tone="amber">
                          {f}
                        </Badge>
                      ))}
                    </div>
                  </div>
                </Link>
              ))}
            </div>
          )}
        </Card>

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
