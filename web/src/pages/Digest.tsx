import { useQuery, useQueryClient } from '@tanstack/react-query'
import { CalendarClock, Loader2, Sparkles } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { Link } from 'react-router'
import { api, fmtDateTime, type Dashboard, type Employee, type Meta } from '../api'
import { MoodWeeks } from '../components/Mood'
import { Badge, Btn, Card, Empty, plural } from '../components/ui'

type Cluster = { name: string; count: number; employees: number; quotes: string[] }
type Digest = {
  id: number
  period: string
  text: string
  created_at: string
  stats: {
    active_newcomers: number
    texts_analyzed: number
    sentiment: Record<string, number>
    clusters: Cluster[]
    avg_scores: Record<string, number>
    questions: number
    answered_by_ai: number
    escalations: number
    red_flags: number
  }
}
type Concern = { reason: string; count: number; urgent: boolean }

// Расписание сводки — backend/app/config.py: digest_weekday = 0 (понедельник), digest_time = "09:00" в поясе
// default_tz (Москва). В Москве UTC+3 круглый год, поэтому 9:00 по Москве — 6:00 UTC.
const MSK = 3 * 3_600_000
const DIGEST_HOUR_UTC = 6
const DAY = 86_400_000

/** Дата ближайшей сводки: понедельник, 9:00 по Москве, ещё не наступивший. */
function nextDigest() {
  const now = Date.now()
  const msk = new Date(now + MSK)
  const toMonday = (8 - msk.getUTCDay()) % 7
  let at = Date.UTC(msk.getUTCFullYear(), msk.getUTCMonth(), msk.getUTCDate() + toMonday, DIGEST_HOUR_UTC)
  if (at <= now) at += 7 * DAY
  return new Date(at).toLocaleDateString('ru-RU', {
    day: 'numeric',
    month: 'long',
    timeZone: 'Europe/Moscow',
  })
}

/** Что беспокоит: главная тревожная причина каждого новичка за 30 дней (та же, что в списке сотрудников). */
function concerns(emps: Employee[]): Concern[] {
  const by = new Map<string, Concern>()
  for (const e of emps) {
    if (!e.signal) continue
    const c = by.get(e.signal.reason) ?? { reason: e.signal.reason, count: 0, urgent: false }
    c.count += 1
    c.urgent ||= e.signal.urgent
    by.set(c.reason, c)
  }
  return [...by.values()].sort((a, b) => Number(b.urgent) - Number(a.urgent) || b.count - a.count)
}

const people = (n: number) => `${n} ${plural(n, 'человек', 'человека', 'человек')}`

function Failed({ retry, busy, children }: { retry: () => void; busy: boolean; children: string }) {
  return (
    <Empty>
      {children}
      <Btn small variant="ghost" onClick={retry} disabled={busy}>
        Повторить
      </Btn>
    </Empty>
  )
}

export default function DigestPage() {
  const qc = useQueryClient()
  const digests = useQuery({ queryKey: ['digests'], queryFn: () => api.get<Digest[]>('/api/digests') })
  const dash = useQuery({
    queryKey: ['dashboard'],
    queryFn: () => api.get<Dashboard>('/api/dashboard'),
    refetchInterval: 15_000,
  })
  const emps = useQuery({ queryKey: ['employees'], queryFn: () => api.get<Employee[]>('/api/employees') })
  const { data: meta } = useQuery({ queryKey: ['meta'], queryFn: () => api.get<Meta>('/api/meta') })
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)
  const run = async () => {
    setBusy(true)
    setFailed(false)
    try {
      await api.post('/api/digests/run')
      qc.invalidateQueries({ queryKey: ['digests'] })
    } catch {
      setFailed(true)
    } finally {
      setBusy(false)
    }
  }
  // Сводку получает в MAX руководитель подбора, подключивший MAX в «Команде» (digest.run → notify_staff).
  const heads = meta?.staff.filter((s) => s.role === 'head') ?? []
  const inMax = !!meta?.max_connected && heads.length > 0 && heads.every((h) => h.max_connected)
  const d = digests.data?.[0]
  const collect = (label: string, small?: boolean) => (
    <Btn small={small} variant={small ? 'ghost' : 'primary'} onClick={run} disabled={busy}>
      {busy ? <Loader2 size={15} className="animate-spin" /> : <Sparkles size={15} />}
      {busy ? 'Собираю…' : label}
    </Btn>
  )
  const runFailed = failed && (
    <div role="alert" className="text-sm text-rose-600">
      Не получилось собрать сводку. Попробуйте ещё раз через минуту.
    </div>
  )
  const list = emps.data && concerns(emps.data)

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-5">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Настроения новичков</h1>
        <p className="mt-1 text-sm text-ink/50">
          Сводка за неделю {inMax ? 'приходит вам в MAX' : 'собирается'} по понедельникам в 9:00 по Москве.
          {meta && !inMax && (
            <>
              {' '}
              Чтобы она приходила вам в MAX, подключите MAX в разделе{' '}
              <Link to="/team" className="text-sky-700 hover:underline">
                «Команда»
              </Link>
              .
            </>
          )}
        </p>
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="Настроение по неделям" subtitle="По ответам новичков в опросах и чате">
          {dash.data ? (
            <MoodWeeks weeks={dash.data.sentiment_weekly} />
          ) : (
            dash.isError && (
              <Failed retry={() => dash.refetch()} busy={dash.isFetching}>
                Не удалось загрузить настроение. Проверьте соединение.
              </Failed>
            )
          )}
        </Card>
        <Card title="Что беспокоит новичков" subtitle="Главная причина тревоги у каждого — за 30 дней">
          {list ? (
            list.length === 0 ? (
              <Empty>За 30 дней тревожных ответов не было</Empty>
            ) : (
              <div className="flex flex-col divide-y divide-ink/[0.06]">
                {list.map((c) => (
                  <div key={c.reason} className="flex items-center justify-between gap-3 py-2.5 text-sm">
                    <span className="first-letter:uppercase">{c.reason}</span>
                    <Badge tone={c.urgent ? 'red' : 'amber'}>{people(c.count)}</Badge>
                  </div>
                ))}
              </div>
            )
          ) : (
            emps.isError && (
              <Failed retry={() => emps.refetch()} busy={emps.isFetching}>
                Не удалось загрузить список. Проверьте соединение.
              </Failed>
            )
          )}
        </Card>
      </div>

      {digests.isError && !digests.data ? (
        <Card title="Сводка за неделю">
          <Failed retry={() => digests.refetch()} busy={digests.isFetching}>
            Не удалось загрузить сводку. Проверьте соединение.
          </Failed>
        </Card>
      ) : !digests.data ? null : !d ? (
        <Card title="Сводка за неделю">
          <Empty icon={<CalendarClock className="text-ink/30" />}>
            Первая сводка — в понедельник, {nextDigest()}, в 9:00
            <span className="text-xs text-ink/40">
              В ней — главное за неделю, темы тревожных ответов с цитатами и оценки в опросах
            </span>
            <div className="mt-2">{collect('Собрать сводку сейчас')}</div>
            {runFailed}
          </Empty>
        </Card>
      ) : (
        <DigestCard d={d} action={collect('Собрать заново', true)} failed={runFailed} />
      )}
    </div>
  )
}

function DigestCard({ d, action, failed }: { d: Digest; action: ReactNode; failed: ReactNode }) {
  const s = d.stats
  const story = d.text.split('🧠')[1]?.trim()
  const facts = [
    `${s.active_newcomers} ${plural(s.active_newcomers, 'новичок', 'новичка', 'новичков')} на адаптации`,
    `${s.texts_analyzed} ${plural(s.texts_analyzed, 'ответ разобран', 'ответа разобрано', 'ответов разобрано')}`,
    s.questions
      ? `Корсик ответил сам на ${s.answered_by_ai} из ${s.questions} ${plural(s.questions, 'вопроса', 'вопросов', 'вопросов')}`
      : 'вопросов не было',
    s.red_flags
      ? `${s.red_flags} ${plural(s.red_flags, 'срочное обращение', 'срочных обращения', 'срочных обращений')}`
      : 'срочных обращений не было',
  ]
  return (
    <Card title={`Сводка за ${d.period}`} subtitle={`Собрана ${fmtDateTime(d.created_at)}`} action={action}>
      <div className="flex flex-col gap-6">
        {failed}
        {story && <p className="text-[15px] leading-relaxed whitespace-pre-wrap text-ink/85">{story}</p>}
        <p className="text-sm text-ink/60">{facts.join(' · ')}</p>
        <section>
          <h3 className="mb-3 text-[13px] text-ink/50">
            Что беспокоило новичков — темы тревожных ответов с цитатами
          </h3>
          {s.clusters.length === 0 ? (
            <Empty>Тревожных тем не было</Empty>
          ) : (
            <div className="grid gap-3 md:grid-cols-2">
              {s.clusters.map((c) => (
                <div key={c.name} className="rounded-2xl border border-ink/[0.07] p-4">
                  <div className="flex items-start justify-between gap-2">
                    <span className="font-medium first-letter:uppercase">{c.name}</span>
                    <Badge tone="amber">{people(c.employees)}</Badge>
                  </div>
                  {c.quotes.slice(0, 2).map((q, i) => (
                    <div key={i} className="mt-2 border-l-2 border-ink/10 pl-3 text-sm text-ink/65 italic">
                      «{q}»
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
        </section>
        {Object.keys(s.avg_scores).length > 0 && (
          <section>
            <h3 className="mb-3 text-[13px] text-ink/50">
              Оценки в опросах — средний балл от 1 до 10; ниже 8 — повод разобраться
            </h3>
            <div className="grid gap-x-8 gap-y-3 md:grid-cols-2">
              {Object.entries(s.avg_scores)
                .sort((a, b) => a[1] - b[1])
                .map(([k, v]) => (
                  <div key={k}>
                    <div className="flex justify-between text-sm">
                      <span className="first-letter:uppercase">{k}</span>
                      <span className={v < 8 ? 'font-semibold text-rose-600' : 'font-semibold'}>
                        {v.toLocaleString('ru-RU')}
                      </span>
                    </div>
                    <div className="mt-1 h-2 rounded-full bg-ink/[0.07]">
                      <div
                        className={`h-2 rounded-full ${v < 8 ? 'bg-rose-500' : 'bg-lime'}`}
                        style={{ width: `${v * 10}%` }}
                      />
                    </div>
                  </div>
                ))}
            </div>
          </section>
        )}
      </div>
    </Card>
  )
}
