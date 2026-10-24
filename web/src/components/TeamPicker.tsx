import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ApiError, api, type Employee, type Meta } from '../api'
import { Badge, Btn, Field, Modal, inputCls } from './ui'

// Руководитель и рекрутер новичка в его карточке. Новые обращения уходят тому рекрутеру, кто указан сейчас:
// сервер берёт получателя из карточки в момент отправки (backend/app/api/team.py, bot/escalation.py).

export default function TeamPicker({ e, onDone }: { e: Employee; onDone: () => void }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <Badge tone={e.manager ? 'gray' : 'amber'}>
        {e.manager ? `руководитель: ${e.manager}` : 'руководитель не назначен'}
      </Badge>
      <Badge tone={e.recruiter ? 'gray' : 'amber'}>
        {e.recruiter ? `рекрутер: ${e.recruiter}` : 'рекрутер не назначен'}
      </Badge>
      <button onClick={() => setOpen(true)} className="px-1 text-xs font-medium text-ink/55 hover:text-ink">
        изменить
      </button>
      {open && (
        <TeamModal
          e={e}
          onClose={() => setOpen(false)}
          onDone={() => {
            setOpen(false)
            onDone()
          }}
        />
      )}
    </>
  )
}

function TeamModal({ e, onClose, onDone }: { e: Employee; onClose: () => void; onDone: () => void }) {
  const qc = useQueryClient()
  const { data: meta } = useQuery({ queryKey: ['meta'], queryFn: () => api.get<Meta>('/api/meta') })
  const [f, setF] = useState({
    manager_id: String(e.manager_id ?? ''),
    recruiter_id: String(e.recruiter_id ?? ''),
  })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const save = async () => {
    setBusy(true)
    setError('')
    try {
      await api.put(`/api/employees/${e.id}/team`, {
        manager_id: f.manager_id ? Number(f.manager_id) : null,
        recruiter_id: f.recruiter_id ? Number(f.recruiter_id) : null,
      })
      qc.invalidateQueries({ queryKey: ['employees'] })
      onDone()
    } catch (err) {
      qc.invalidateQueries({ queryKey: ['meta'] }) // список людей мог устареть, пока окно было открыто
      setError(
        err instanceof ApiError && err.detail ? err.detail : 'Не получилось сохранить. Попробуйте ещё раз.',
      )
    } finally {
      setBusy(false)
    }
  }
  const select = (key: keyof typeof f, role: string) => (
    <select className={inputCls} value={f[key]} onChange={(ev) => setF({ ...f, [key]: ev.target.value })}>
      <option value="">не выбран</option>
      {meta?.staff
        .filter((s) => s.role === role)
        .map((s) => (
          <option key={s.id} value={s.id}>
            {s.name}
          </option>
        ))}
    </select>
  )
  return (
    <Modal open onClose={onClose} title={`Команда: ${e.full_name}`}>
      <div className="flex flex-col gap-4">
        <Field label="Руководитель" hint="Получает напоминания и советы, когда поговорить с новичком">
          {select('manager_id', 'manager')}
        </Field>
        <Field label="Рекрутер" hint="Получает обращения новичка, которые Корсик передаёт людям">
          {select('recruiter_id', 'recruiter')}
        </Field>
        <p className="text-xs text-ink/50">Нет нужного человека — добавьте его на экране «Команда».</p>
        {error && (
          <div role="alert" className="rounded-xl bg-rose-50 px-4 py-3 text-sm text-rose-700">
            {error}
          </div>
        )}
        <div className="flex justify-end gap-2">
          <Btn variant="ghost" onClick={onClose}>
            Отмена
          </Btn>
          <Btn onClick={save} disabled={busy}>
            {busy ? 'Сохраняю…' : 'Сохранить'}
          </Btn>
        </div>
      </div>
    </Modal>
  )
}
