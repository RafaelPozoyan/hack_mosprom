import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Copy, Download, FileSpreadsheet, Plus, Search, UserPlus } from 'lucide-react'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import { api, fmtDate, probationDay, withToken, type Employee, type Meta } from '../api'
import EmployeeImport from '../components/EmployeeImport'
import {
  Avatar,
  Badge,
  Btn,
  Card,
  Empty,
  Field,
  Modal,
  Progress,
  inputCls,
  plural,
  siteName,
  timeAgo,
} from '../components/ui'

const startsIn = (days: number) =>
  days === 1 ? 'выходит завтра' : `выходит через ${days} ${plural(days, 'день', 'дня', 'дней')}`

export function StageCell({ e }: { e: Pick<Employee, 'status' | 'start_date' | 'probation_end'> }) {
  const { day, total } = probationDay(e)
  if (e.status === 'invited') return <span className="text-sm text-ink/50">приглашение ещё не открыто</span>
  if (day < 1) return <span className="text-sm text-ink/60">{startsIn(1 - day)}</span>
  return (
    <div className="w-40">
      <div className="mb-1 text-xs text-ink/55">
        день {Math.min(day, total)} из {total}
      </div>
      <Progress value={(100 * day) / total} />
    </div>
  )
}

export default function Employees() {
  const qc = useQueryClient()
  const nav = useNavigate()
  const { data: emps = [] } = useQuery({
    queryKey: ['employees'],
    queryFn: () => api.get<Employee[]>('/api/employees'),
    refetchInterval: 15_000,
  })
  const { data: meta } = useQuery({ queryKey: ['meta'], queryFn: () => api.get<Meta>('/api/meta') })
  const [open, setOpen] = useState(false)
  const [importing, setImporting] = useState(false)
  const [q, setQ] = useState('')
  const list = emps.filter((e) => `${e.full_name} ${e.position}`.toLowerCase().includes(q.toLowerCase()))

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">Сотрудники на адаптации</h1>
        <div className="flex gap-2">
          <Btn variant="ghost" onClick={() => setImporting(true)}>
            <FileSpreadsheet size={16} /> Загрузить из Excel
          </Btn>
          <Btn onClick={() => setOpen(true)}>
            <Plus size={16} /> Добавить новичка
          </Btn>
        </div>
      </div>
      <Card>
        <div className="relative mb-3 max-w-sm">
          <Search size={16} className="absolute top-1/2 left-3 -translate-y-1/2 text-ink/35" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Поиск по имени или должности"
            className={`${inputCls} w-full pl-9`}
          />
        </div>
        {list.length === 0 ? (
          <Empty icon={<UserPlus className="text-ink/30" />}>
            {emps.length
              ? 'Никого не нашлось'
              : 'Добавьте первого новичка или загрузите список из Excel — Корсик сам проведёт каждого по пути адаптации'}
          </Empty>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-ink/45">
              <tr className="border-b border-ink/[0.07]">
                <th className="py-2.5 font-medium">Сотрудник</th>
                <th className="font-medium">Площадка</th>
                <th className="font-medium">Первый день</th>
                <th className="font-medium">Где сейчас</th>
                <th className="text-right font-medium">Статус</th>
              </tr>
            </thead>
            <tbody>
              {list.map((e) => (
                <tr
                  key={e.id}
                  className="cursor-pointer border-b border-ink/[0.04] transition hover:bg-ink/[0.02]"
                  onClick={() => nav(`/employees/${e.id}`)}
                >
                  <td className="py-3">
                    <div className="flex items-center gap-3">
                      <Avatar name={e.full_name} size={34} />
                      <div>
                        <div className="font-medium">{e.full_name}</div>
                        <div className="text-xs text-ink/50">{e.position}</div>
                      </div>
                    </div>
                  </td>
                  <td className="text-ink/70">{siteName(e.site_code)}</td>
                  <td className="text-ink/70 tabular-nums">{fmtDate(e.start_date)}</td>
                  <td>
                    <StageCell e={e} />
                  </td>
                  <td className="text-right">
                    <div className="flex justify-end gap-1">
                      {e.signal && (
                        <Badge tone={e.signal.urgent ? 'red' : 'amber'}>
                          {e.signal.reason} · {timeAgo(e.signal.at)}
                        </Badge>
                      )}
                      {e.open_ticket && <Badge tone="red">ждёт ответа HR</Badge>}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
      {importing && (
        <EmployeeImport
          onClose={() => setImporting(false)}
          onDone={() => qc.invalidateQueries({ queryKey: ['employees'] })}
        />
      )}
      {meta && (
        <CreateModal
          open={open}
          meta={meta}
          onClose={() => setOpen(false)}
          onDone={(e) => {
            qc.invalidateQueries({ queryKey: ['employees'] })
            setOpen(false)
            nav(`/employees/${e.id}`)
          }}
        />
      )}
    </div>
  )
}

function CreateModal({
  open,
  meta,
  onClose,
  onDone,
}: {
  open: boolean
  meta: Meta
  onClose: () => void
  onDone: (e: Employee) => void
}) {
  const empty = {
    full_name: '',
    position: '',
    site_code: meta.sites[0]?.code ?? '',
    start_date: '',
    manager_id: '',
    recruiter_id: '',
  }
  const [f, setF] = useState(empty)
  const [created, setCreated] = useState<Employee | null>(null)
  const m = useMutation({
    mutationFn: () =>
      api.post<Employee>('/api/employees', {
        ...f,
        manager_id: f.manager_id ? Number(f.manager_id) : null,
        recruiter_id: f.recruiter_id ? Number(f.recruiter_id) : null,
      }),
    onSuccess: setCreated,
  })
  const set = (k: keyof typeof empty) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setF({ ...f, [k]: e.target.value })
  const close = () => {
    setCreated(null)
    setF(empty)
    onClose()
  }
  if (created)
    return (
      <InviteModal
        e={created}
        onClose={() => {
          onDone(created)
          setCreated(null)
          setF(empty)
        }}
      />
    )
  return (
    <Modal open={open} onClose={close} title="Новый сотрудник">
      <form
        className="grid grid-cols-2 gap-4"
        onSubmit={(e) => {
          e.preventDefault()
          m.mutate()
        }}
      >
        <div className="col-span-2">
          <Field label="Фамилия и имя">
            <input
              required
              autoFocus
              placeholder="Иванова Анна"
              className={inputCls}
              value={f.full_name}
              onChange={set('full_name')}
            />
          </Field>
        </div>
        <Field label="Должность">
          <input
            className={inputCls}
            placeholder="Инженер-технолог"
            value={f.position}
            onChange={set('position')}
          />
        </Field>
        <Field label="Дата выхода">
          <input
            required
            type="date"
            className={inputCls}
            value={f.start_date}
            onChange={set('start_date')}
          />
        </Field>
        <div className="col-span-2">
          <Field
            label="Площадка"
            hint="От неё зависят тексты Корсика: документы, транспорт, карта, справочник"
          >
            <select className={inputCls} value={f.site_code} onChange={set('site_code')}>
              {meta.sites.map((s) => (
                <option key={s.code} value={s.code}>
                  {siteName(s.code)}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Field label="Руководитель">
          <select className={inputCls} value={f.manager_id} onChange={set('manager_id')}>
            <option value="">не выбран</option>
            {meta.staff
              .filter((s) => s.role === 'manager')
              .map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
          </select>
        </Field>
        <Field label="Рекрутер">
          <select className={inputCls} value={f.recruiter_id} onChange={set('recruiter_id')}>
            <option value="">не выбран</option>
            {meta.staff
              .filter((s) => s.role === 'recruiter')
              .map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
          </select>
        </Field>
        <div className="col-span-2 mt-1 flex justify-end gap-2">
          <Btn variant="ghost" onClick={close}>
            Отмена
          </Btn>
          <Btn type="submit" disabled={m.isPending}>
            {m.isPending ? 'Создаю…' : 'Создать и получить QR'}
          </Btn>
        </div>
        {m.error && (
          <div className="col-span-2 text-sm text-rose-600">
            Не получилось сохранить. Проверьте поля и попробуйте ещё раз.
          </div>
        )}
      </form>
    </Modal>
  )
}

export function InviteModal({ e, onClose }: { e: Employee; onClose: () => void }) {
  const [copied, setCopied] = useState(false)
  const qr = withToken(`/api/employees/${e.id}/qr.svg`)
  return (
    <Modal open onClose={onClose} title={`Приглашение: ${e.full_name}`}>
      <div className="flex flex-col items-center gap-4 text-center">
        <img src={qr} alt="QR-код" className="size-52 rounded-2xl border border-ink/10 p-2" />
        <p className="max-w-sm text-sm text-ink/60">
          Добавьте QR-код в оффер или отправьте ссылку. Новичок откроет Корсика в MAX — и бот сам проведёт его
          от первого дня до конца испытательного срока.
        </p>
        <div className="flex flex-wrap justify-center gap-2">
          <a href={qr} download={`QR ${e.full_name}.svg`}>
            <Btn variant="ghost">
              <Download size={15} /> Скачать QR
            </Btn>
          </a>
          {e.invite_link && (
            <Btn
              variant="ghost"
              onClick={() => {
                navigator.clipboard.writeText(e.invite_link!)
                setCopied(true)
              }}
            >
              {copied ? <Check size={15} /> : <Copy size={15} />}{' '}
              {copied ? 'Скопировано' : 'Скопировать ссылку'}
            </Btn>
          )}
          <Btn onClick={onClose}>Готово</Btn>
        </div>
      </div>
    </Modal>
  )
}
