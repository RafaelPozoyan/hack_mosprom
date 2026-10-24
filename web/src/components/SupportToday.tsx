import { useQuery } from '@tanstack/react-query'
import clsx from 'clsx'
import { ChevronDown, HeartHandshake } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router'
import { api, type SupportPerson, type SupportReason } from '../api'
import { Avatar, Badge, Btn, Card, Empty, timeAgo } from './ui'

const FROM: Record<string, string> = { опрос: 'из опроса', сообщение: 'из сообщения' }
const SHOWN = 3 // остальные — под «Ещё N», чтобы блок не вытеснял остальное со страницы

/** Причина тревоги одной строкой: что случилось, где новичок об этом сказал и когда. */
export function Reason({ r }: { r: SupportReason }) {
  return (
    <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
      <Badge tone={r.urgent ? 'red' : 'amber'}>{r.reason}</Badge>
      <span className="text-xs text-ink/45">
        {FROM[r.source] ?? r.source} · {timeAgo(r.at)}
      </span>
    </span>
  )
}

export const useSupportToday = () =>
  useQuery({
    queryKey: ['support-today'],
    queryFn: () => api.get<SupportPerson[]>('/api/support/today'),
    refetchInterval: 15_000,
  })

/** Блок «Сегодня»: все новички с тревожными сигналами за сутки, срочные первыми (порядок задаёт сервер).
 *  В строке — главная причина; остальные причины и действие — в карточке новичка. */
export default function SupportToday() {
  const { data, isError, refetch, isFetching } = useSupportToday()
  const [all, setAll] = useState(false)
  return (
    <Card title="Кому сегодня нужна поддержка" subtitle="Тревожные ответы новичков за последние сутки">
      {isError && !data ? (
        <Empty>
          Не удалось загрузить список. Проверьте соединение.
          <Btn small variant="ghost" onClick={() => refetch()} disabled={isFetching}>
            Повторить
          </Btn>
        </Empty>
      ) : !data ? null : data.length === 0 ? (
        <Empty icon={<HeartHandshake className="text-ink/30" />}>
          Сегодня всё спокойно
          <span className="text-xs text-ink/40">Если новичок ответит тревожно, он появится здесь</span>
        </Empty>
      ) : (
        <>
          <div className="flex flex-col divide-y divide-ink/[0.06]">
            {(all ? data : data.slice(0, SHOWN)).map((p) => (
              <Link
                key={p.employee_id}
                to={`/employees/${p.employee_id}`}
                className="-mx-2 flex items-center gap-3 rounded-xl px-2 py-3 hover:bg-ink/[0.025]"
              >
                <Avatar name={p.name} size={36} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 font-medium">
                    {p.name}
                    {p.urgent && <Badge tone="red">срочно</Badge>}
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-2">
                    <Reason r={p.reasons[0]} />
                    {p.reasons.length > 1 && (
                      <span className="text-xs text-ink/45">и ещё {p.reasons.length - 1} в карточке</span>
                    )}
                  </div>
                </div>
              </Link>
            ))}
          </div>
          {data.length > SHOWN && (
            <button
              type="button"
              onClick={() => setAll(!all)}
              className="mt-2 flex items-center gap-1 text-[13px] text-ink/55 hover:text-ink/80"
            >
              {all ? 'Свернуть' : `Ещё ${data.length - SHOWN}`}
              <ChevronDown size={14} className={clsx('transition', all && 'rotate-180')} />
            </button>
          )}
        </>
      )}
    </Card>
  )
}
