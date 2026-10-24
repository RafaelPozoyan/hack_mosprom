import clsx from 'clsx'
import type { Dashboard } from '../api'
import { Empty, SENTIMENT, plural } from './ui'

type Week = Dashboard['sentiment_weekly'][number]

const PARTS = [
  { key: 'positive', color: 'bg-lime' },
  { key: 'neutral', color: 'bg-ink/20' },
  { key: 'negative', color: 'bg-rose-500' },
] as const

const DAY = 86_400_000
const total = (w: Week) => w.positive + w.neutral + w.negative

/** Понедельник недели weeksBack недель назад, «2026-10-19». Неделя считается по UTC, как в metrics.dashboard. */
function monday(weeksBack = 0) {
  const now = new Date()
  const today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())
  return new Date(today - (((now.getUTCDay() + 6) % 7) + 7 * weeksBack) * DAY).toISOString().slice(0, 10)
}

const since = (week: string) =>
  `с ${new Date(week).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', timeZone: 'UTC' })}`

function anxious(w: Week) {
  const n = w.negative
  const all = total(w)
  return n
    ? `${n} ${plural(n, 'тревожный ответ', 'тревожных ответа', 'тревожных ответов')} из ${all}`
    : `тревожных ответов нет, всего ${all} ${plural(all, 'ответ', 'ответа', 'ответов')}`
}

/** Полоса на 100%: доли хороших, спокойных и тревожных ответов за неделю. */
function Strip({ w }: { w: Week }) {
  return (
    <div className="flex h-2.5 gap-0.5 overflow-hidden rounded-full">
      {PARTS.map(
        (p) =>
          w[p.key] > 0 && (
            <div key={p.key} className={clsx('basis-0', p.color)} style={{ flexGrow: w[p.key] }} />
          ),
      )}
    </div>
  )
}

/** Подписи частей полосы; с неделей — с числами: «хорошее 1 · спокойное 10 · тревожное 3». */
function Legend({ w }: { w?: Week }) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-[13px] text-ink/60">
      {PARTS.map((p) => (
        <span key={p.key} className="flex items-center gap-1.5">
          <span className={clsx('size-2 rounded-full', p.color)} />
          {SENTIMENT[p.key].label}
          {w && <span className="font-medium text-ink tabular-nums">{w[p.key]}</span>}
        </span>
      ))}
    </div>
  )
}

/** «Сегодня»: последняя неделя с ответами — фраза с числом и полоса. */
export function MoodWeek({ weeks }: { weeks: Week[] }) {
  const w = weeks.at(-1)
  if (!w) return <Empty>Ответов пока нет — настроение появится после первых опросов</Empty>
  return (
    <div className="flex flex-col gap-3">
      <p className="text-[15px]">
        {w.week < monday() && <span className="text-ink/60">На этой неделе ответов пока нет. </span>}
        <span className="font-medium">Неделя {since(w.week)}:</span> {anxious(w)}
      </p>
      <Strip w={w} />
      <Legend w={w} />
    </div>
  )
}
