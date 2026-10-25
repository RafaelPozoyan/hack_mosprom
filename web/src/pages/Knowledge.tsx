import { useQuery, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { FileText, FileUp, Loader2, MessageCircleQuestion, Trash2 } from 'lucide-react'
import { useRef, useState } from 'react'
import { api, fmtDateTime, type Meta } from '../api'
import { Badge, Btn, Card, Empty, Modal, inputCls, siteName, useDemo } from '../components/ui'

type Doc = {
  id: number
  title: string
  filename: string
  site_codes: string[]
  status: string
  error: string | null
  n_chunks: number
  created_at: string
}
type Debug = {
  hits: { chunk_id: number; title: string; heading: string; text: string; score: number }[]
  answered: boolean
  answer: string
  reason: string
  sources: { title: string; heading: string; quote: string }[]
  threshold: number
}

// Почему Корсик не ответил сам, простыми словами. Коды — из backend/app/rag/answer.py.
// missing — ответа нет в документах: HR может загрузить документ, где он есть.
const REASON: Record<string, { text: string; missing?: boolean }> = {
  no_hits: { text: 'в документах нет ответа', missing: true },
  low_score: { text: 'в документах нет ответа', missing: true },
  not_in_context: { text: 'в документах нет ответа', missing: true },
  bad_citation: { text: 'ответ не подтвердился цитатой — лучше спросить HR' },
  unsupported_number: { text: 'в ответе было число, которого нет в документе — лучше спросить HR' },
  unsupported_contact: {
    text: 'в ответе была почта или ссылка, которых нет в документе — лучше спросить HR',
  },
  llm_error: { text: 'сервис не отвечает — попробуйте через минуту' },
  search_error: { text: 'сервис не отвечает — попробуйте через минуту' },
}

export default function Knowledge() {
  const qc = useQueryClient()
  const { data: docs = [] } = useQuery({
    queryKey: ['docs'],
    queryFn: () => api.get<Doc[]>('/api/documents'),
    refetchInterval: 5000,
  })
  const { data: meta } = useQuery({ queryKey: ['meta'], queryFn: () => api.get<Meta>('/api/meta') })
  const [peek, setPeek] = useState<Doc | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const [sites, setSites] = useState<string[]>([])
  const [drag, setDrag] = useState(false)

  const upload = async (files: FileList | null) => {
    for (const f of Array.from(files ?? [])) {
      const fd = new FormData()
      fd.append('file', f)
      fd.append('title', f.name.replace(/\.[^.]+$/, ''))
      fd.append('sites', sites.join(','))
      await api.post('/api/documents', fd)
    }
    qc.invalidateQueries({ queryKey: ['docs'] })
  }
  const remove = async (d: Doc) => {
    if (!confirm(`Удалить «${d.title}»? Корсик перестанет отвечать по этому документу.`)) return
    await api.del(`/api/documents/${d.id}`)
    qc.invalidateQueries({ queryKey: ['docs'] })
  }

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-5">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">База знаний</h1>
        <p className="mt-1 text-sm text-ink/50">
          Корсик отвечает новичкам только по этим документам — и всегда показывает, откуда взял ответ.
        </p>
      </div>
      <div className="grid gap-5 lg:grid-cols-[1fr_1fr]">
        <div className="flex flex-col gap-5">
          <Card>
            <div
              onDragOver={(e) => {
                e.preventDefault()
                setDrag(true)
              }}
              onDragLeave={() => setDrag(false)}
              onDrop={(e) => {
                e.preventDefault()
                setDrag(false)
                upload(e.dataTransfer.files)
              }}
              onClick={() => fileRef.current?.click()}
              className={clsx(
                'flex cursor-pointer flex-col items-center gap-2 rounded-2xl border-2 border-dashed p-8 text-center transition',
                drag ? 'border-lime bg-lime/10' : 'border-ink/15 hover:border-ink/30 hover:bg-ink/[0.015]',
              )}
            >
              <FileUp className="text-ink/40" />
              <div className="text-sm font-medium">Перетащите документ или нажмите, чтобы выбрать</div>
              <div className="text-xs text-ink/50">
                PDF, Word, PowerPoint, текст. Сканы и картинки на слайдах Корсик тоже прочитает.
              </div>
              <input ref={fileRef} type="file" multiple hidden onChange={(e) => upload(e.target.files)} />
            </div>
            <div className="mt-4 flex flex-wrap items-center gap-2 text-[13px]">
              <span className="text-ink/55">Для кого:</span>
              <button
                onClick={() => setSites([])}
                className={clsx(
                  'rounded-full px-3 py-1',
                  sites.length === 0 ? 'bg-ink text-white' : 'bg-ink/[0.06]',
                )}
              >
                все площадки
              </button>
              {meta?.sites.map((s) => (
                <button
                  key={s.code}
                  onClick={() =>
                    setSites((p) => (p.includes(s.code) ? p.filter((x) => x !== s.code) : [...p, s.code]))
                  }
                  className={clsx(
                    'rounded-full px-3 py-1',
                    sites.includes(s.code) ? 'bg-ink text-white' : 'bg-ink/[0.06]',
                  )}
                >
                  {siteName(s.code)}
                </button>
              ))}
            </div>
          </Card>
          <Card title={`Документы · ${docs.length}`}>
            {docs.length === 0 ? (
              <Empty>Загрузите первый документ — например, правила внутреннего распорядка</Empty>
            ) : (
              <div className="-mx-2 flex flex-col">
                {docs.map((d) => (
                  <div
                    key={d.id}
                    className="group flex items-center gap-3 rounded-xl px-2 py-2.5 hover:bg-ink/[0.025]"
                  >
                    <FileText size={18} className="shrink-0 text-ink/35" />
                    <button
                      onClick={() => d.status === 'ready' && setPeek(d)}
                      className="min-w-0 flex-1 text-left"
                    >
                      <div className="truncate text-sm font-medium">{d.title}</div>
                      <div className="text-xs text-ink/45">
                        {d.site_codes.length ? d.site_codes.map(siteName).join(', ') : 'все площадки'} ·{' '}
                        {fmtDateTime(d.created_at)}
                      </div>
                    </button>
                    {d.status === 'ready' && <Badge tone="green">готов</Badge>}
                    {d.status === 'processing' && (
                      <Badge tone="amber">
                        <Loader2 size={12} className="animate-spin" /> читаю…
                      </Badge>
                    )}
                    {d.status === 'error' && <Badge tone="red">не удалось прочитать</Badge>}
                    <button
                      onClick={() => remove(d)}
                      className="text-ink/25 opacity-0 transition group-hover:opacity-100 hover:text-rose-600"
                      title="Удалить"
                    >
                      <Trash2 size={16} />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </div>
        <Ask />
      </div>
      {peek && <Peek doc={peek} onClose={() => setPeek(null)} />}
    </div>
  )
}

function Peek({ doc, onClose }: { doc: Doc; onClose: () => void }) {
  const { data = [] } = useQuery({
    queryKey: ['chunks', doc.id],
    queryFn: () =>
      api.get<{ id: number; heading: string; text: string }[]>(`/api/documents/${doc.id}/chunks`),
  })
  return (
    <Modal open onClose={onClose} title={`Что Корсик прочитал: ${doc.title}`} wide>
      <div className="flex max-h-[65vh] flex-col gap-3 overflow-y-auto pr-1">
        {data.map((c) => (
          <div key={c.id} className="rounded-xl bg-ink/[0.03] p-3 text-sm">
            <div className="mb-1 text-xs font-semibold text-ink/50">
              {c.heading.split(' › ').slice(1).join(' › ') || doc.title}
            </div>
            <div className="whitespace-pre-wrap text-ink/80">{c.text}</div>
          </div>
        ))}
      </div>
    </Modal>
  )
}

function Ask() {
  const demo = useDemo()
  const [q, setQ] = useState('')
  const [res, setRes] = useState<Debug | null>(null)
  const [busy, setBusy] = useState(false)
  const why = res ? REASON[res.reason] : undefined
  const run = async (question = q) => {
    if (!question.trim()) return
    setQ(question)
    setBusy(true)
    setRes(null)
    try {
      setRes(await api.post<Debug>('/api/rag/debug', { question, site: '' }))
    } finally {
      setBusy(false)
    }
  }
  // Ответы есть в открытой памятке (backend/kb): примеры работают и без документов компании
  const examples = [
    'Когда руководитель передаст адаптационный план?',
    'Какой у меня испытательный срок?',
    'Кто поможет, если возникнут вопросы?',
  ]
  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <MessageCircleQuestion size={18} /> Проверить ответ Корсика
        </span>
      }
      subtitle="Спросите так, как спросил бы новичок"
    >
      <div className="flex gap-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && run()}
          placeholder="Например: какие документы нужны для оформления?"
          className={clsx(inputCls, 'flex-1')}
        />
        <Btn onClick={() => run()} disabled={busy || !q.trim()}>
          {busy ? 'Думаю…' : 'Спросить'}
        </Btn>
      </div>
      {!res && !busy && (
        <div className="mt-3 flex flex-wrap gap-2">
          {examples.map((e) => (
            <button
              key={e}
              onClick={() => run(e)}
              className="rounded-full bg-ink/[0.05] px-3 py-1.5 text-xs hover:bg-ink/10"
            >
              {e}
            </button>
          ))}
        </div>
      )}
      {busy && (
        <div className="mt-6 flex items-center gap-2 text-sm text-ink/50">
          <Loader2 size={16} className="animate-spin" /> Корсик ищет ответ в документах…
        </div>
      )}
      {res && (
        <div className="mt-5 flex flex-col gap-3">
          {res.answered ? (
            <div className="rounded-2xl bg-lime/15 p-4 text-sm">
              <div className="whitespace-pre-wrap">{res.answer}</div>
              <div className="mt-3 flex flex-col gap-2">
                {res.sources.map((s, i) => (
                  <div key={i} className="border-l-2 border-lime-2 pl-3 text-xs text-ink/65">
                    <div className="font-medium text-ink/75">{s.heading.split(' › ').join(' → ')}</div>
                    <div className="italic">«{s.quote}»</div>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div className="rounded-2xl bg-amber-50 p-4 text-sm">
              <div className="font-medium">Корсик не станет отвечать сам и предложит передать вопрос HR</div>
              <div className="mt-1 text-ink/60">
                Причина: {why?.text ?? 'не удалось подобрать надёжный ответ'}.
                {why?.missing && ' Если ответ должен быть — загрузите документ, где он есть.'}
              </div>
            </div>
          )}
          {demo && (
            <details className="rounded-xl border border-dashed border-sky/50 bg-sky-50/40 p-3 text-xs">
              <summary className="cursor-pointer font-medium text-sky-800">
                Как Корсик искал (для специалистов)
              </summary>
              <div className="mt-2 text-ink/55">
                Гибридный поиск: полнотекстовый + смысловой (BGE-M3), объединение RRF, переранжирование
                bge-reranker-v2-m3. Порог уверенности {res.threshold}.
              </div>
              {res.hits.map((h, i) => (
                <div key={h.chunk_id} className="mt-2 rounded-lg bg-white p-2">
                  <div className="flex justify-between gap-2">
                    <span className="font-medium">
                      {i + 1}. {h.heading}
                    </span>
                    <span className="tabular-nums">{h.score.toFixed(3)}</span>
                  </div>
                  <div className="mt-1 line-clamp-2 text-ink/55">{h.text}</div>
                </div>
              ))}
            </details>
          )}
        </div>
      )}
    </Card>
  )
}
