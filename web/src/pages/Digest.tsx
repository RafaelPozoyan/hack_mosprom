import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Sparkles } from 'lucide-react'
import { useState } from 'react'
import { api, fmtDateTime } from '../api'
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

export default function DigestPage() {
  const qc = useQueryClient()
  const { data = [] } = useQuery({ queryKey: ['digests'], queryFn: () => api.get<Digest[]>('/api/digests') })
  const [busy, setBusy] = useState(false)
  const run = async () => {
    setBusy(true)
    try {
      await api.post('/api/digests/run')
      qc.invalidateQueries({ queryKey: ['digests'] })
    } finally {
      setBusy(false)
    }
  }
  const d = data[0]
  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Настроения в коллективе новичков</h1>
          <p className="mt-1 text-sm text-ink/50">
            Еженедельный дайджест приходит вам в MAX по понедельникам в 9:00
          </p>
        </div>
        <Btn onClick={run} disabled={busy}>
          {busy ? <Loader2 size={15} className="animate-spin" /> : <Sparkles size={15} />}{' '}
          {busy ? 'Готовлю…' : 'Собрать за последнюю неделю'}
        </Btn>
      </div>
      {!d ? (
        <Card>
          <Empty>Дайджестов пока нет — нажмите «Собрать», чтобы получить первый</Empty>
        </Card>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            <Mini label="Новичков на адаптации" value={d.stats.active_newcomers} />
            <Mini label="Ответов проанализировано" value={d.stats.texts_analyzed} />
            <Mini
              label="Корсик ответил сам"
              value={d.stats.questions ? `${d.stats.answered_by_ai} из ${d.stats.questions}` : '—'}
            />
            <Mini label="Срочных сигналов" value={d.stats.red_flags} red={d.stats.red_flags > 0} />
          </div>
          {d.text.includes('🧠') && (
            <Card
              title="Главное за неделю"
              subtitle={`Период ${d.period} · собран ${fmtDateTime(d.created_at)}`}
            >
              <p className="text-[15px] leading-relaxed whitespace-pre-wrap text-ink/85">
                {d.text.split('🧠')[1].trim()}
              </p>
            </Card>
          )}
          <Card title="Что беспокоит новичков" subtitle="Корсик сгруппировал свободные ответы по темам">
            {d.stats.clusters.length === 0 ? (
              <Empty>Тревожных тем не найдено</Empty>
            ) : (
              <div className="grid gap-3 md:grid-cols-2">
                {d.stats.clusters.map((c) => (
                  <div key={c.name} className="rounded-2xl border border-ink/[0.07] p-4">
                    <div className="flex items-start justify-between gap-2">
                      <span className="font-medium first-letter:uppercase">{c.name}</span>
                      <Badge tone="amber">
                        {c.employees} {plural(c.employees, 'человек', 'человека', 'человек')}
                      </Badge>
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
          </Card>
          {Object.keys(d.stats.avg_scores).length > 0 && (
            <Card title="Оценки в опросах" subtitle="Средний балл от 1 до 10; ниже 8 — повод разобраться">
              <div className="grid gap-x-8 gap-y-3 md:grid-cols-2">
                {Object.entries(d.stats.avg_scores)
                  .sort((a, b) => a[1] - b[1])
                  .map(([k, v]) => (
                    <div key={k}>
                      <div className="flex justify-between text-sm">
                        <span className="first-letter:uppercase">{k}</span>
                        <span className={v < 8 ? 'font-semibold text-rose-600' : 'font-semibold'}>{v}</span>
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
            </Card>
          )}
        </>
      )}
    </div>
  )
}

function Mini({ label, value, red }: { label: string; value: React.ReactNode; red?: boolean }) {
  return (
    <div className="rounded-2xl border border-ink/[0.07] bg-white p-4">
      <div className="text-[13px] text-ink/55">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${red ? 'text-rose-600' : ''}`}>{value}</div>
    </div>
  )
}
