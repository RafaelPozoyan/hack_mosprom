import clsx from 'clsx'
import { FastForward, FileText, QrCode, Send } from 'lucide-react'
import { Fragment, useEffect, useRef, useState, type ReactNode } from 'react'
import { api, fmtTime, type ChatMessage, type Employee } from '../api'

const URL_RE = /(\[[^\]]+\]\([^)]+\)|https?:\/\/[^\s)]+|\*\*[^*]+\*\*)/g

function RichText({ text }: { text: string }) {
  const parts = text.split(URL_RE)
  return (
    <>
      {parts.map((p, i) => {
        const md = p.match(/^\[([^\]]+)\]\(([^)]+)\)$/)
        if (md)
          return (
            <a key={i} href={md[2]} target="_blank" rel="noreferrer" className="text-sky underline">
              {md[1]}
            </a>
          )
        if (/^https?:\/\//.test(p))
          return (
            <a key={i} href={p} target="_blank" rel="noreferrer" className="break-all text-sky underline">
              {p}
            </a>
          )
        if (/^\*\*.+\*\*$/.test(p)) return <strong key={i}>{p.slice(2, -2)}</strong>
        return <Fragment key={i}>{p}</Fragment>
      })}
    </>
  )
}

function Bubble({
  m,
  onButton,
}: {
  m: ChatMessage
  onButton: (payload: string, text: string, id: number) => void
}) {
  const mine = m.author === 'employee'
  const p = m.payload ?? {}
  if (mine && p.button) {
    return (
      <div className="my-1 flex justify-end">
        <span className="rounded-full bg-ink/8 px-3 py-1 text-xs text-ink/70">нажата кнопка «{m.text}»</span>
      </div>
    )
  }
  return (
    <div className={clsx('my-1.5 flex', mine ? 'justify-end' : 'justify-start')}>
      <div className={clsx('max-w-[85%]')}>
        <div
          data-author={m.author}
          className={clsx(
            'rounded-2xl px-3 py-2 text-[13.5px] leading-snug whitespace-pre-wrap shadow-sm',
            mine
              ? 'rounded-br-md bg-[#d9f2a6] text-ink'
              : m.author === 'hr'
                ? 'rounded-bl-md bg-sky-100 text-ink'
                : 'rounded-bl-md bg-white text-ink',
          )}
        >
          {m.author === 'hr' && <div className="mb-0.5 text-[11px] font-semibold text-sky-700">Живой HR</div>}
          {p.image && <img src={`/media/${p.image}`} alt="" className="mb-2 w-full rounded-xl" />}
          {m.text && <RichText text={m.text} />}
          {p.file && (
            <a
              href={`/media/${p.file}`}
              target="_blank"
              rel="noreferrer"
              className="mt-2 flex items-center gap-2 rounded-xl bg-ink/5 px-2.5 py-2 text-xs font-medium"
            >
              <FileText size={16} /> Открыть вложение
            </a>
          )}
          {p.sources && p.sources.length > 0 && (
            <details className="mt-1.5 text-[11.5px] text-ink/60">
              <summary className="cursor-pointer select-none">
                Цитаты из документов ({p.sources.length})
              </summary>
              {p.sources.map((s, i) => (
                <div key={i} className="mt-1 border-l-2 border-lime pl-2">
                  <div className="font-medium text-ink/70">{s.heading}</div>
                  <div className="italic">«{s.quote}»</div>
                </div>
              ))}
            </details>
          )}
        </div>
        {p.buttons && p.buttons.length > 0 && (
          <div className="mt-1 flex flex-col gap-1">
            {p.buttons.map((row, r) => (
              <div key={r} className="flex gap-1">
                {row.map((b, c) =>
                  b.url ? (
                    <a
                      key={c}
                      href={b.url}
                      target="_blank"
                      rel="noreferrer"
                      className="flex-1 rounded-xl bg-white/90 px-2 py-1.5 text-center text-[12.5px] font-medium text-sky-700 shadow-sm"
                    >
                      {b.text} ↗
                    </a>
                  ) : (
                    <button
                      key={c}
                      onClick={() => onButton(b.payload!, b.text, m.id)}
                      className="flex-1 rounded-xl bg-white/90 px-2 py-1.5 text-[12.5px] font-medium text-ink shadow-sm transition hover:bg-lime/30 active:scale-[0.98]"
                    >
                      {b.text}
                    </button>
                  ),
                )}
              </div>
            ))}
          </div>
        )}
        <div className={clsx('mt-0.5 text-[10px] text-ink/40', mine ? 'text-right' : 'text-left')}>
          {fmtTime(m.created_at)}
        </div>
      </div>
    </div>
  )
}

export default function ChatPhone({
  employee,
  onActivity,
  footer,
}: {
  employee: Employee
  onActivity?: () => void
  footer?: ReactNode
}) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [text, setText] = useState('')
  const [typing, setTyping] = useState(false)
  const [nextInfo, setNextInfo] = useState<string | null>(null)
  const scroller = useRef<HTMLDivElement>(null)
  const base = `/api/demo/employees/${employee.id}`

  useEffect(() => {
    let alive = true
    api.get<ChatMessage[]>(`${base}/messages`).then((ms) => alive && setMessages(ms))
    const es = api.stream(`${base}/stream`)
    es.onmessage = (e) => {
      const ev = JSON.parse(e.data)
      if (!['employee', 'bot', 'hr'].includes(ev.message?.author)) return
      if (ev.type === 'message') {
        setMessages((prev) => (prev.some((x) => x.id === ev.message.id) ? prev : [...prev, ev.message]))
        if (ev.message.author !== 'employee') setTyping(false)
      }
      if (ev.type === 'edit')
        setMessages((prev) => prev.map((x) => (x.id === ev.message.id ? ev.message : x)))
      onActivity?.()
    }
    return () => {
      alive = false
      es.close()
    }
  }, [base, onActivity])

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: 'smooth' })
  }, [messages, typing])

  const busy = () => {
    setTyping(true)
    setTimeout(() => setTyping(false), 120_000)
  }
  const send = async () => {
    if (!text.trim()) return
    const t = text
    setText('')
    busy()
    await api.post(`${base}/message`, { text: t })
  }
  const press = async (payload: string, btnText: string, id: number) => {
    busy()
    await api.post(`${base}/callback`, { payload, text: btnText, message_id: id })
  }
  const next = async () => {
    busy()
    try {
      const r = await api.post<{ ok: boolean; status?: string; run_at?: string }>(`${base}/next`)
      const status: Record<string, string> = {
        await_start: 'Ждём запуска бота по QR',
        done: 'Шаг выполнен',
        skipped: 'Шаг пропущен',
        blocked: 'Ждём обязательный ответ',
        pending: 'Шаг ждёт повторной отправки',
        failed: 'Не удалось выполнить шаг',
        running: 'Шаг ещё обрабатывается',
      }
      const result = status[r.status ?? ''] ?? 'Проверьте состояние шага'
      setNextInfo(
        r.ok
          ? `${result} · по плану ${new Date(r.run_at!).toLocaleDateString('ru-RU', { timeZone: 'Europe/Moscow' })}`
          : 'Запланированных шагов больше нет',
      )
      onActivity?.()
    } catch {
      setNextInfo('Не удалось подтвердить результат. Проверьте путь адаптации перед повтором.')
    } finally {
      setTyping(false)
    }
  }

  return (
    <div className="flex flex-col items-center gap-3">
      <div className="flex h-[680px] w-[360px] flex-col overflow-hidden rounded-[38px] border-[10px] border-ink bg-[#e9eef0] shadow-2xl">
        <div className="flex items-center gap-2.5 bg-gradient-to-r from-ink to-ink-2 px-4 pt-4 pb-3 text-white">
          <div className="grid size-9 place-items-center rounded-full bg-lime text-lg">🐾</div>
          <div className="leading-tight">
            <div className="text-sm font-semibold">Корсик</div>
            <div className="text-[11px] text-white/70">{typing ? 'печатает…' : 'бот в MAX'}</div>
          </div>
        </div>
        <div ref={scroller} className="flex-1 overflow-y-auto px-3 py-2">
          {messages.length === 0 && (
            <div className="mt-24 text-center text-xs text-ink/50">
              {employee.status === 'invited'
                ? 'Сотрудник ещё не запустил бота. Нажмите «Старт по QR».'
                : 'Сообщений пока нет'}
            </div>
          )}
          {messages.map((m) => (
            <Bubble key={m.id} m={m} onButton={press} />
          ))}
          {typing && (
            <div className="my-1.5 inline-flex gap-1 rounded-2xl rounded-bl-md bg-white px-3 py-2.5 shadow-sm">
              {[0, 1, 2].map((i) => (
                <span
                  key={i}
                  className="size-1.5 animate-bounce rounded-full bg-ink/40"
                  style={{ animationDelay: `${i * 0.15}s` }}
                />
              ))}
            </div>
          )}
        </div>
        <form
          className="flex items-center gap-2 border-t border-ink/10 bg-white px-3 py-2.5"
          onSubmit={(e) => {
            e.preventDefault()
            send()
          }}
        >
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Сообщение"
            className="flex-1 rounded-full bg-ink/5 px-3.5 py-2 text-sm outline-none"
          />
          <button type="submit" className="grid size-9 place-items-center rounded-full bg-ink text-white">
            <Send size={16} />
          </button>
        </form>
      </div>
      <div className="flex w-[360px] flex-wrap justify-center gap-2">
        {employee.status === 'invited' && (
          <button
            onClick={() => {
              busy()
              api.post(`${base}/start`)
            }}
            className="inline-flex items-center gap-1.5 rounded-xl border border-dashed border-sky/60 bg-sky-50 px-3 py-2 text-sm font-medium text-sky-800"
          >
            <QrCode size={16} /> Новичок сканирует QR
          </button>
        )}
        <button
          onClick={next}
          className="inline-flex items-center gap-1.5 rounded-xl border border-dashed border-sky/60 bg-sky-50 px-3 py-2 text-sm font-medium text-sky-800"
        >
          <FastForward size={16} /> Перемотать к следующему шагу
        </button>
      </div>
      {nextInfo && <div className="w-[360px] text-center text-xs text-ink/55">{nextInfo}</div>}
      {footer}
    </div>
  )
}
