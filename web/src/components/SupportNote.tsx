import { useMutation, useQuery } from '@tanstack/react-query'
import clsx from 'clsx'
import { CheckCircle2, ShieldCheck, UserRoundPlus } from 'lucide-react'
import { useState } from 'react'
import { ApiError, api, type EmployeeSupport } from '../api'
import { Reason } from './SupportToday'
import { Btn, Modal } from './ui'

type Props = { empId: number; name: string; onSent: () => void }

/** Причины тревоги новичка за месяц и можно ли подключить руководителя. Тот же ответ сервера читает вкладка
 *  «Что беспокоит» — срочность причин там такая же, как в шапке. */
export const useSupport = (empId: number) =>
  useQuery({
    queryKey: ['employee-support', empId],
    queryFn: () => api.get<EmployeeSupport>(`/api/employees/${empId}/support`),
    refetchInterval: 15_000,
  })

/** Тревожные сигналы новичка в шапке карточки и одно действие — подключить руководителя.
 *  Можно ли подключить, решает сервер; здесь причина отказа видна заранее. */
export default function SupportNote({ empId, name, onSent }: Props) {
  const { data } = useSupport(empId)
  const [open, setOpen] = useState(false)
  const [delivered, setDelivered] = useState<boolean | null>(null)
  if (!data?.reasons.length) return null
  const sent = delivered
    ? 'Отправлено в MAX'
    : data.manager_in_max
      ? 'В MAX не доставлено — рекомендация на экране «Мои новички»'
      : 'Отправлено на экран «Мои новички»'
  return (
    <div
      className={clsx(
        'mt-4 flex flex-wrap items-start justify-between gap-3 rounded-xl border px-4 py-3',
        data.today ? 'border-amber-200' : 'border-ink/10',
      )}
    >
      <div className="min-w-0">
        <div className="mb-2 text-sm font-medium">
          {data.today ? 'Нужна поддержка сегодня' : 'Тревожные сигналы за месяц'}
        </div>
        <div className="flex flex-col gap-1.5">
          {data.reasons.slice(0, 3).map((r) => (
            <Reason key={`${r.reason}:${r.source}`} r={r} />
          ))}
        </div>
      </div>
      <div className="flex flex-col items-end gap-1">
        <Btn
          variant={data.today ? 'primary' : 'ghost'}
          disabled={!!data.refusal}
          onClick={() => setOpen(true)}
        >
          <UserRoundPlus size={16} /> Подключить руководителя
        </Btn>
        {data.refusal && <span className="max-w-64 text-right text-xs text-ink/50">{data.refusal}</span>}
        {delivered !== null && (
          <span role="status" className="flex max-w-64 gap-1 text-right text-xs text-emerald-700">
            <CheckCircle2 size={14} className="mt-px shrink-0" /> {sent}
          </span>
        )}
      </div>
      {open && (
        <SendModal
          data={data}
          empId={empId}
          name={name}
          onClose={() => setOpen(false)}
          onDone={(ok) => {
            setDelivered(ok)
            setOpen(false)
            onSent()
          }}
        />
      )}
    </div>
  )
}

/** Выбор рекомендации: текст сообщения раскрывается у выбранного варианта — ровно то, что получит руководитель. */
function SendModal({
  data,
  empId,
  name,
  onClose,
  onDone,
}: {
  data: EmployeeSupport
  empId: number
  name: string
  onClose: () => void
  onDone: (delivered: boolean) => void
}) {
  const [choice, setChoice] = useState(data.options[0]?.id)
  const send = useMutation({
    mutationFn: () =>
      api.post<{ delivered: boolean }>(`/api/employees/${empId}/support/manager`, { option: choice }),
    onSuccess: (r) => onDone(r.delivered),
  })
  return (
    <Modal open onClose={onClose} title="Подключить руководителя" wide>
      <div className="flex flex-col gap-4">
        <p className="text-sm text-ink/60">
          Кому: <span className="font-medium text-ink">{data.manager}</span>,{' '}
          {data.manager_in_max ? 'в MAX' : 'на экран «Мои новички»'}
        </p>
        <div className="flex flex-col gap-2">
          {data.options.map((o) => (
            <label
              key={o.id}
              className={clsx(
                'cursor-pointer rounded-xl border px-4 py-3 text-sm transition',
                o.id === choice ? 'border-ink/30 bg-lime/10' : 'border-ink/10 hover:bg-ink/[0.02]',
              )}
            >
              <span className="flex items-center gap-3 font-medium">
                <input
                  type="radio"
                  className="accent-lime"
                  checked={o.id === choice}
                  onChange={() => setChoice(o.id)}
                />
                {o.title}
              </span>
              {o.id === choice && (
                <span className="mt-2 block rounded-lg bg-white px-3 py-2 text-[13px] whitespace-pre-wrap text-ink/75">
                  <span className="mb-1 block text-xs font-medium text-sky-800">Корсик · {name}</span>
                  {o.text}
                </span>
              )}
            </label>
          ))}
        </div>
        <p className="flex items-center gap-2 text-xs text-ink/50">
          <ShieldCheck size={14} /> Ответы новичка руководитель не увидит
        </p>
        {send.isError && (
          <div role="alert" className="text-sm text-rose-600">
            {send.error instanceof ApiError && send.error.detail
              ? send.error.detail
              : 'Не удалось отправить. Проверьте соединение и попробуйте ещё раз.'}
          </div>
        )}
        <div className="flex justify-end gap-2">
          <Btn variant="ghost" onClick={onClose}>
            Отмена
          </Btn>
          <Btn onClick={() => send.mutate()} disabled={!choice || send.isPending}>
            {send.isPending ? 'Отправляю…' : 'Отправить'}
          </Btn>
        </div>
      </div>
    </Modal>
  )
}
