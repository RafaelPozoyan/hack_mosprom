import clsx from 'clsx'
import { CheckCircle2, Download, FileSpreadsheet, Loader2 } from 'lucide-react'
import { useRef, useState } from 'react'
import { ApiError, api, withToken } from '../api'
import { Badge, Btn, Empty, Modal, plural, siteName, type Tone } from './ui'

// Загрузка новичков файлом из кадровой системы: сервер разбирает файл и показывает, что будет с каждой строкой,
// а записывает только после подтверждения. Ключ строки — табельный номер (backend/app/api/hr_import.py).

type Status = 'new' | 'update' | 'unchanged' | 'error'
type Row = {
  row: number
  personnel_number: string
  full_name: string
  position: string
  site_code: string | null
  start_date: string | null
  status: Status
  errors: string[]
  warnings: string[]
  changes: string[]
}
type Counts = Record<Status, number>

// Порядок — что HR важнее увидеть первым: ошибки и изменения, затем новые строки.
const STATUS: Record<Status, { label: string; tone: Tone }> = {
  error: { label: 'ошибка', tone: 'red' },
  update: { label: 'обновится', tone: 'blue' },
  new: { label: 'новый', tone: 'green' },
  unchanged: { label: 'без изменений', tone: 'gray' },
}
const ORDER = Object.keys(STATUS) as Status[]
const FAILED = 'Не получилось обработать файл. Попробуйте ещё раз или перенесите данные в шаблон.'

async function upload<T>(path: string, file: File) {
  const fd = new FormData()
  fd.append('file', file)
  return api.post<T>(path, fd)
}

export default function EmployeeImport({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const input = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<{ rows: Row[]; counts: Counts } | null>(null)
  const [result, setResult] = useState<Counts | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [drag, setDrag] = useState(false)
  const [showAll, setShowAll] = useState(false)

  const run = async (task: () => Promise<void>) => {
    setBusy(true)
    setError('')
    try {
      await task()
    } catch (e) {
      setError(e instanceof ApiError && e.detail ? e.detail : FAILED)
    } finally {
      setBusy(false)
    }
  }
  const choose = (f?: File) =>
    f &&
    run(async () => {
      setFile(f)
      setShowAll(false)
      setPreview(await upload('/api/employees/import/preview', f))
    })
  const load = () =>
    file &&
    run(async () => {
      setResult(await upload('/api/employees/import', file))
      onDone()
    })
  const reset = () => {
    setPreview(null)
    setError('')
    if (input.current) input.current.value = ''
  }

  const rows = [...(preview?.rows ?? [])].sort(
    (a, b) => ORDER.indexOf(a.status) - ORDER.indexOf(b.status) || b.warnings.length - a.warnings.length,
  )
  const shown = showAll ? rows : rows.filter((r) => r.status !== 'unchanged')
  const ready = preview ? preview.counts.new + preview.counts.update : 0
  const errorBox = error && (
    <div role="alert" className="mt-4 rounded-xl bg-rose-50 px-4 py-3 text-sm text-rose-700">
      {error}
    </div>
  )

  if (result) {
    const done = [result.new && `добавлено ${result.new}`, result.update && `обновлено ${result.update}`]
    return (
      <Modal open onClose={onClose} title="Новички загружены">
        <div className="flex flex-col items-center gap-3 text-center">
          <CheckCircle2 size={36} className="text-lime-2" />
          <p className="max-w-sm text-sm text-ink/70 first-letter:uppercase">
            {done.filter(Boolean).join(', ') || 'Ничего не изменилось'}. Корсик напишет новичку, как только
            тот откроет приглашение. QR-код — в карточке новичка.
          </p>
          <Btn onClick={onClose}>Готово</Btn>
        </div>
      </Modal>
    )
  }

  return (
    <Modal open onClose={onClose} title="Загрузка новичков из файла" wide>
      {!preview ? (
        <>
          <div
            onDragOver={(e) => {
              e.preventDefault()
              setDrag(true)
            }}
            onDragLeave={() => setDrag(false)}
            onDrop={(e) => {
              e.preventDefault()
              setDrag(false)
              choose(e.dataTransfer.files[0])
            }}
            onClick={() => !busy && input.current?.click()}
            className={clsx(
              'flex cursor-pointer flex-col items-center gap-2 rounded-2xl border-2 border-dashed p-8 text-center transition',
              drag ? 'border-lime bg-lime/10' : 'border-ink/15 hover:border-ink/30 hover:bg-ink/[0.015]',
            )}
          >
            {busy ? (
              <Loader2 className="animate-spin text-ink/40" />
            ) : (
              <FileSpreadsheet className="text-ink/40" />
            )}
            <div className="text-sm font-medium">
              {busy ? 'Читаю файл…' : 'Перетащите файл или нажмите, чтобы выбрать'}
            </div>
            <div className="text-xs text-ink/50">Excel или CSV — например, выгрузка из 1С</div>
            <input
              ref={input}
              type="file"
              accept=".xlsx,.csv"
              hidden
              onChange={(e) => choose(e.target.files?.[0])}
            />
          </div>
          <details className="mt-4 text-[13px] text-ink/60">
            <summary className="cursor-pointer font-medium text-ink/70">Какие колонки нужны</summary>
            <p className="mt-2">
              Обязательно: табельный номер, ФИО, площадка и дата выхода. По желанию: должность, подразделение,
              руководитель, рекрутер. По табельному номеру Корсик узнаёт новичка, поэтому повторная загрузка
              не создаст дублей. До записи покажем, что будет с каждой строкой.
            </p>
          </details>
          {errorBox}
          <div className="mt-5 flex justify-between gap-2">
            <a
              href={withToken('/api/employees/import/template.xlsx')}
              download="Шаблон загрузки новичков.xlsx"
            >
              <Btn variant="ghost">
                <Download size={15} /> Скачать шаблон
              </Btn>
            </a>
            <Btn variant="ghost" onClick={onClose}>
              Отмена
            </Btn>
          </div>
        </>
      ) : (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-1.5 text-sm">
            <span className="mr-1 truncate text-ink/60">{file?.name}</span>
            {ORDER.filter((s) => preview.counts[s]).map((s) => (
              <Badge key={s} tone={STATUS[s].tone}>
                {STATUS[s].label}: {preview.counts[s]}
              </Badge>
            ))}
          </div>
          {shown.length === 0 ? (
            <Empty>Все новички из файла уже загружены — менять нечего.</Empty>
          ) : (
            <div className="max-h-[52vh] overflow-y-auto rounded-xl border border-ink/[0.07]">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-white text-left text-xs text-ink/45">
                  <tr className="border-b border-ink/[0.07]">
                    <th className="px-3 py-2 font-medium">Строка</th>
                    <th className="font-medium">Новичок</th>
                    <th className="px-3 font-medium">Выход</th>
                    <th className="px-3 text-right font-medium">Что будет</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((r) => (
                    <tr key={r.row} className="border-b border-ink/[0.04] align-top last:border-0">
                      <td className="px-3 py-2.5 text-ink/45 tabular-nums">{r.row}</td>
                      <td className="py-2.5">
                        <div className="font-medium">{r.full_name || 'ФИО не указано'}</div>
                        <div className="text-xs text-ink/50">
                          {[r.position, r.site_code && siteName(r.site_code)].filter(Boolean).join(' · ')}
                        </div>
                        {[
                          ...r.errors.map((t) => [t, 'text-rose-600']),
                          ...r.changes.map((t) => [t, 'text-sky-800']),
                          ...r.warnings.map((t) => [t, 'text-amber-700']),
                        ].map(([t, cls]) => (
                          <div key={t} className={clsx('mt-1 text-xs', cls)}>
                            {t}
                          </div>
                        ))}
                      </td>
                      <td className="px-3 py-2.5 text-ink/70 tabular-nums">
                        {r.start_date?.split('-').reverse().join('.') ?? '—'}
                      </td>
                      <td className="px-3 py-2.5 text-right">
                        <Badge tone={STATUS[r.status].tone}>{STATUS[r.status].label}</Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {!showAll && preview.counts.unchanged > 0 && shown.length > 0 && (
            <button onClick={() => setShowAll(true)} className="mt-2 text-[13px] text-ink/55 hover:text-ink">
              Показать ещё {preview.counts.unchanged} без изменений
            </button>
          )}
          {preview.counts.error > 0 && (
            <p className="mt-3 text-[13px] text-ink/60">
              Строки с ошибкой не загрузятся — исправьте их в файле и загрузите его снова. Дублей не будет.
            </p>
          )}
          {errorBox}
          <div className="mt-5 flex justify-end gap-2">
            <Btn variant="ghost" onClick={reset} disabled={busy}>
              Другой файл
            </Btn>
            <Btn onClick={load} disabled={busy || !ready}>
              {busy
                ? 'Загружаю…'
                : ready
                  ? `Загрузить ${ready} ${plural(ready, 'новичка', 'новичков', 'новичков')}`
                  : 'Нечего загружать'}
            </Btn>
          </div>
        </>
      )}
    </Modal>
  )
}
