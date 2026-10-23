import { useQuery } from '@tanstack/react-query'
import { BellRing, CalendarClock, Lightbulb, ShieldCheck } from 'lucide-react'
import { Link, useParams } from 'react-router'
import { api, fmtDateTime, type Employee, type ManagerCardData, type Meta } from '../api'
import { Avatar, Card, Empty, siteName, timeAgo } from '../components/ui'
import { StageCell } from './Employees'

// Руководитель видит прогресс и рекомендации по своим новичкам. Свободные ответы и сигналы,
// касающиеся самого руководителя, сюда не попадают — их видит только HR (политика в сценарии).

export default function ManagerView() {
  const { data: meta } = useQuery({ queryKey: ['meta'], queryFn: () => api.get<Meta>('/api/meta') })
  const { data: emps = [] } = useQuery({ queryKey: ['employees'], queryFn: () => api.get<Employee[]>('/api/employees') })
  const manager = meta?.staff.find((s) => s.role === 'manager')
  const mine = emps.filter((e) => manager && e.manager_id === manager.id)
  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-5">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Мои новички</h1>
        {manager && <p className="mt-1 text-sm text-ink/50">{manager.name}</p>}
      </div>
      {mine.length === 0 ? (
        <Card><Empty>Новых сотрудников в вашей команде пока нет</Empty></Card>
      ) : (
        mine.map((e) => (
          <Link key={e.id} to={`/my/${e.id}`}>
            <Card className="transition hover:shadow-md">
              <div className="flex flex-wrap items-center gap-4">
                <Avatar name={e.full_name} size={44} />
                <div className="min-w-0 flex-1">
                  <div className="font-semibold">{e.full_name}</div>
                  <div className="text-sm text-ink/55">{e.position} · {siteName(e.site_code)}</div>
                </div>
                <StageCell e={e} />
              </div>
            </Card>
          </Link>
        ))
      )}
      <p className="flex items-center gap-2 text-xs text-ink/45">
        <ShieldCheck size={14} /> Личные ответы новичков видит только HR — вам приходят рекомендации и напоминания.
      </p>
    </div>
  )
}

export function ManagerCard() {
  const { id } = useParams()
  const { data } = useQuery({ queryKey: ['manager-card', id], queryFn: () => api.get<ManagerCardData>(`/api/manager/employees/${id}`) })
  if (!data) return null
  const e = data.employee
  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-5">
      <Card>
        <div className="flex flex-wrap items-center gap-4">
          <Avatar name={e.full_name} size={56} />
          <div className="min-w-0 flex-1">
            <h1 className="text-xl font-semibold tracking-tight">{e.full_name}</h1>
            <div className="text-sm text-ink/55">{e.position} · {siteName(e.site_code)}</div>
          </div>
          <StageCell e={e} />
        </div>
      </Card>
      <Card title={<span className="flex items-center gap-2"><Lightbulb size={17} className="text-amber-500" /> Рекомендации Корсика</span>}>
        {data.signals.length === 0 ? (
          <Empty>Всё идёт по плану — дополнительных действий не нужно</Empty>
        ) : (
          <div className="flex flex-col gap-2">
            <p className="text-sm text-ink/70">Проведите короткую встречу 1:1 в ближайшие дни. Что стоит обсудить:</p>
            {data.signals.map((s) => <div key={s} className="rounded-xl bg-amber-50 px-3 py-2 text-sm first-letter:uppercase">{s}</div>)}
          </div>
        )}
      </Card>
      <div className="grid gap-5 md:grid-cols-2">
        <Card title={<span className="flex items-center gap-2"><CalendarClock size={17} /> Что дальше</span>}>
          {data.upcoming.length === 0 ? <Empty>Адаптация завершена</Empty> : (
            <div className="flex flex-col gap-2.5">
              {data.upcoming.map((u) => (
                <div key={u.title} className="flex justify-between gap-3 text-sm">
                  <span>{u.title}</span>
                  <span className="shrink-0 text-ink/45">{fmtDateTime(u.run_at)}</span>
                </div>
              ))}
            </div>
          )}
        </Card>
        <Card title={<span className="flex items-center gap-2"><BellRing size={17} /> Уведомления вам</span>}>
          {data.notifications.length === 0 ? <Empty>Пока не было</Empty> : (
            <div className="flex flex-col gap-2">
              {data.notifications.map((n, i) => (
                <div key={i} className="rounded-xl bg-sky-50/60 p-3 text-sm">
                  <div className="mb-0.5 text-xs text-ink/45">{timeAgo(n.at)}</div>
                  <div className="whitespace-pre-wrap">{n.text}</div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}
