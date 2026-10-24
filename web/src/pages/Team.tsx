import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Copy, QrCode, UserPlus } from 'lucide-react'
import { useState } from 'react'
import { ApiError, api, withToken, type Meta } from '../api'
import { Avatar, Badge, Btn, Card, Empty, Field, Modal, inputCls, plural } from '../components/ui'

// Команда адаптации: кому Корсик пишет о новичках. Сервер — backend/app/api/team.py.

type Role = 'recruiter' | 'manager' | 'head'
type Member = Meta['staff'][number] & { newcomers: number }

const GROUPS: { role: Role; title: string; name: string; hint: string; empty: string }[] = [
  {
    role: 'recruiter',
    title: 'Рекрутеры',
    name: 'Рекрутер',
    hint: 'Получают обращения своих новичков',
    empty: 'Добавьте рекрутера — ему будут приходить обращения новичков',
  },
  {
    role: 'manager',
    title: 'Руководители подразделений',
    name: 'Руководитель подразделения',
    hint: 'Получают напоминания и советы по своим новичкам',
    empty: 'Добавьте руководителя — Корсик подскажет, когда поговорить с новичком',
  },
  {
    role: 'head',
    title: 'Руководитель подбора',
    name: 'Руководитель подбора',
    hint: 'Получает еженедельный отчёт о настроениях новичков',
    empty: 'Добавьте руководителя подбора — ему будет приходить еженедельный отчёт',
  },
]

const failure = (e: unknown) =>
  e instanceof ApiError && e.detail ? e.detail : 'Не получилось сохранить. Попробуйте ещё раз.'

export default function Team() {
  const qc = useQueryClient()
  const { data: members = [] } = useQuery({
    queryKey: ['team'],
    queryFn: () => api.get<Member[]>('/api/team'),
    refetchInterval: 10_000, // видно, как человек подключился к MAX по QR
  })
  const [editing, setEditing] = useState<Member | 'new' | null>(null)
  const [inviting, setInviting] = useState<Member | null>(null)
  const saved = (m?: Member) => {
    qc.invalidateQueries({ queryKey: ['team'] })
    qc.invalidateQueries({ queryKey: ['meta'] })
    if (editing === 'new' && m) setInviting(m)
    setEditing(null)
  }

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Команда</h1>
          <p className="mt-1 text-sm text-ink/50">Кому Корсик пишет о новичках</p>
        </div>
        <Btn onClick={() => setEditing('new')}>
          <UserPlus size={16} /> Добавить человека
        </Btn>
      </div>
      {GROUPS.map((g) => {
        const people = members.filter((m) => m.role === g.role)
        return (
          <Card key={g.role} title={g.title} subtitle={g.hint}>
            {people.length === 0 ? (
              <Empty>{g.empty}</Empty>
            ) : (
              <div className="-mx-2 flex flex-col">
                {people.map((m) => (
                  <div
                    key={m.id}
                    className="flex items-center gap-3 rounded-xl px-2 py-2 hover:bg-ink/[0.025]"
                  >
                    <button
                      onClick={() => setEditing(m)}
                      className="flex min-w-0 flex-1 items-center gap-3 text-left"
                      title="Изменить"
                    >
                      <Avatar name={m.name} size={36} />
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium">{m.name}</div>
                        {g.role !== 'head' && (
                          <div className="text-xs text-ink/50">
                            {m.newcomers
                              ? `${m.newcomers} ${plural(m.newcomers, 'новичок', 'новичка', 'новичков')}`
                              : 'новичков пока нет'}
                          </div>
                        )}
                      </div>
                    </button>
                    {m.max_connected ? (
                      <Badge tone="green">
                        <Check size={12} /> получает уведомления в MAX
                      </Badge>
                    ) : (
                      <Btn small variant="ghost" onClick={() => setInviting(m)}>
                        <QrCode size={13} /> Подключить к MAX
                      </Btn>
                    )}
                  </div>
                ))}
              </div>
            )}
          </Card>
        )
      })}
      {editing && (
        <MemberModal
          member={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={saved}
        />
      )}
      {inviting && <Invite m={inviting} onClose={() => setInviting(null)} />}
    </div>
  )
}

function MemberModal({
  member,
  onClose,
  onSaved,
}: {
  member: Member | null
  onClose: () => void
  onSaved: (m?: Member) => void
}) {
  const [name, setName] = useState(member?.name ?? '')
  const [role, setRole] = useState<Role>((member?.role as Role) ?? 'recruiter')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const run = async (task: () => Promise<Member | undefined>) => {
    setBusy(true)
    setError('')
    try {
      onSaved(await task())
    } catch (e) {
      setError(failure(e))
    } finally {
      setBusy(false)
    }
  }
  const body = { name, role }
  return (
    <Modal open onClose={onClose} title={member ? 'Изменить' : 'Новый человек в команде'}>
      <form
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault()
          run(() =>
            member ? api.patch<Member>(`/api/team/${member.id}`, body) : api.post<Member>('/api/team', body),
          )
        }}
      >
        <Field label="Имя и фамилия">
          <input
            required
            autoFocus
            placeholder="Мария Кузнецова"
            className={inputCls}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </Field>
        <Field label="Роль" hint={GROUPS.find((g) => g.role === role)?.hint}>
          <select className={inputCls} value={role} onChange={(e) => setRole(e.target.value as Role)}>
            {GROUPS.map((g) => (
              <option key={g.role} value={g.role}>
                {g.name}
              </option>
            ))}
          </select>
        </Field>
        {error && (
          <div role="alert" className="rounded-xl bg-rose-50 px-4 py-3 text-sm text-rose-700">
            {error}
          </div>
        )}
        <div className="mt-1 flex justify-between gap-2">
          {member ? (
            <Btn
              variant="ghost"
              disabled={busy}
              onClick={() =>
                confirm(`Убрать ${member.name} из команды?`) &&
                run(async () => {
                  await api.del(`/api/team/${member.id}`)
                  return undefined
                })
              }
            >
              Убрать из команды
            </Btn>
          ) : (
            <span />
          )}
          <div className="flex gap-2">
            <Btn variant="ghost" onClick={onClose}>
              Отмена
            </Btn>
            <Btn type="submit" disabled={busy}>
              {member ? 'Сохранить' : 'Добавить'}
            </Btn>
          </div>
        </div>
      </form>
    </Modal>
  )
}

function Invite({ m, onClose }: { m: Member; onClose: () => void }) {
  const [copied, setCopied] = useState(false)
  return (
    <Modal open onClose={onClose} title={`Подключить к MAX: ${m.name}`}>
      {m.invite_link ? (
        <div className="flex flex-col items-center gap-4 text-center">
          <img
            src={withToken(`/api/team/${m.id}/qr.svg`)}
            alt="QR-код"
            className="size-52 rounded-2xl border border-ink/10 p-2"
          />
          <p className="max-w-sm text-sm text-ink/60">
            Пусть {m.name} отсканирует код телефоном или откроет ссылку — Корсик начнёт присылать уведомления
            в MAX.
          </p>
          <div className="flex flex-wrap justify-center gap-2">
            <Btn
              variant="ghost"
              onClick={() => {
                navigator.clipboard.writeText(m.invite_link!)
                setCopied(true)
              }}
            >
              {copied ? <Check size={15} /> : <Copy size={15} />}{' '}
              {copied ? 'Скопировано' : 'Скопировать ссылку'}
            </Btn>
            <Btn onClick={onClose}>Готово</Btn>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          <Empty>Бот MAX ещё не настроен — ссылка для подключения появится после настройки.</Empty>
          <div className="flex justify-end">
            <Btn onClick={onClose}>Понятно</Btn>
          </div>
        </div>
      )}
    </Modal>
  )
}
