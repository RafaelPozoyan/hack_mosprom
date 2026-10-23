import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Bell, ClipboardList, MessageCircle, Pencil } from 'lucide-react'
import { useState } from 'react'
import { PHASES, api, type Meta } from '../api'
import { Badge, Btn, Card, Modal, inputCls, siteName } from '../components/ui'

type Step = Meta['steps'][number]

const when = (at: Step['at']) => {
  if (!at) return ''
  if (at.event) return 'сразу после запуска по QR'
  const t = at.time ? `, ${at.time}` : ''
  if (at.day !== undefined) return `за ${Math.abs(Number(at.day))} дн. до выхода${t}`
  if (at.workday !== undefined) return `${at.workday}-й рабочий день${t}`
  if (at.month_start !== undefined) return `начало ${at.month_start}-го месяца${t}`
  if (at.probation_end) return `последний день испытательного срока${t}`
  const parts = [at.months && `${at.months} мес.`, at.weeks && `${at.weeks} нед.`, at.days && `${at.days} дн.`].filter(Boolean).join(' ')
  return `через ${parts} после выхода${t}`
}

export default function Scenario() {
  const { data: meta } = useQuery({ queryKey: ['meta'], queryFn: () => api.get<Meta>('/api/meta') })
  const [edit, setEdit] = useState<Step | null>(null)
  if (!meta) return null
  const timed = meta.steps.filter((s) => s.at)
  const phases = PHASES.filter((p) => timed.some((s) => s.phase === p))
  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-5">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Путь новичка</h1>
        <p className="mt-1 text-sm text-ink/50">
          Что и когда Корсик делает сам — от оффера до конца испытательного срока. Тексты учитывают площадку и то, как новичок попросил к нему обращаться.
        </p>
      </div>
      <div className="grid gap-5 lg:grid-cols-[1fr_300px]">
        <div className="flex flex-col gap-4">
          {phases.map((p) => (
            <Card key={p} title={p}>
              <ol className="flex flex-col">
                {timed.filter((s) => s.phase === p).map((s, i) => (
                  <li key={i} onClick={() => setEdit(s)} className="group -mx-2 flex cursor-pointer items-start gap-3 rounded-xl border-b border-ink/[0.05] px-2 py-2.5 last:border-0 hover:bg-ink/[0.025]">
                    {s.audience === 'employee' ? <MessageCircle size={17} className="mt-0.5 shrink-0 text-lime-2" /> : <Bell size={17} className="mt-0.5 shrink-0 text-sky" />}
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5 text-sm">
                        <span className="font-medium">{s.title}</span>
                        {s.audience === 'manager' && <Badge tone="blue">руководителю</Badge>}
                        {s.audience === 'recruiter' && <Badge tone="blue">рекрутеру</Badge>}
                        {s.manual && <Badge tone="amber">по команде рекрутера</Badge>}
                        {s.sites.map((x) => <Badge key={x}>{siteName(x)}</Badge>)}
                      </div>
                      <div className="mt-0.5 text-xs text-ink/45">{when(s.at)}</div>
                    </div>
                    <Pencil size={15} className="mt-1 shrink-0 text-ink/25 opacity-0 transition group-hover:opacity-100" />
                  </li>
                ))}
              </ol>
            </Card>
          ))}
        </div>
        <div className="flex flex-col gap-5">
          <Card title={<span className="flex items-center gap-2"><ClipboardList size={17} /> Опросы</span>}>
            {Object.entries(meta.surveys).map(([id, s]) => (
              <div key={id} className="flex items-center justify-between gap-2 border-b border-ink/[0.05] py-2 text-sm last:border-0">
                <span>{s.title}</span>
                <span className="shrink-0 text-xs text-ink/45">{s.questions} вопр.</span>
              </div>
            ))}
            <p className="mt-3 text-xs text-ink/45">Если оценка ниже 8 из 10, Корсик просит рассказать причину. Если опрос не пройден — напоминает на следующий день и через день.</p>
          </Card>
          <Card title="В любой момент">
            <ul className="flex flex-col gap-2 text-sm text-ink/70">
              <li>Отвечает на вопросы по документам базы знаний</li>
              <li>/food — где пообедать</li>
              <li>/hr — написать HR через Корсика</li>
              <li>Сложный вопрос — передаёт живому HR в том же чате</li>
            </ul>
          </Card>
          <Card title="Подключение">
            <div className="flex flex-col gap-2 text-sm">
              <div className="flex items-center justify-between"><span>Мессенджер MAX</span><Badge tone={meta.max_connected ? 'green' : 'amber'}>{meta.max_connected ? 'подключён' : 'демо-чат'}</Badge></div>
            </div>
          </Card>
        </div>
      </div>
      {edit && <StepEditor step={edit} onClose={() => setEdit(null)} />}
    </div>
  )
}

type StepDetail = { id: string; title: string; sites: string[]; messages: { index: number; text: string | null; ai: string | null; survey: string | null; image: string | null; buttons: string[] }[] }

const preview = (t: string, vy: boolean) => t.replace(/\{([^{}|]*)\|([^{}|]*)\}/g, (_, a, b) => (vy ? b : a)).replaceAll('{name}', 'Анна')

function StepEditor({ step, onClose }: { step: Step; onClose: () => void }) {
  const qc = useQueryClient()
  const key = ['step', step.id, step.sites.join(',')]
  const { data } = useQuery({ queryKey: key, queryFn: () => api.get<StepDetail>(`/api/scenario/steps/${step.id}?sites=${step.sites.join(',')}`) })
  const [texts, setTexts] = useState<Record<number, string>>({})
  const [vy, setVy] = useState(false)
  const [saved, setSaved] = useState(false)
  const save = async () => {
    await api.put(`/api/scenario/steps/${step.id}`, { sites: step.sites, texts })
    qc.invalidateQueries({ queryKey: key })
    qc.invalidateQueries({ queryKey: ['meta'] })
    setSaved(true)
    setTimeout(onClose, 700)
  }
  return (
    <Modal open onClose={onClose} title={step.title} wide>
      {!data ? null : (
        <div className="flex flex-col gap-4">
          <div className="flex items-center justify-between text-xs text-ink/50">
            <span>{when(step.at) || 'по кнопке или команде'}{step.sites.length ? ` · ${step.sites.map(siteName).join(', ')}` : ''}</span>
            <label className="flex cursor-pointer items-center gap-1.5"><input type="checkbox" checked={vy} onChange={(e) => setVy(e.target.checked)} className="accent-lime" /> предпросмотр на «вы»</label>
          </div>
          <div className="flex max-h-[60vh] flex-col gap-4 overflow-y-auto pr-1">
            {data.messages.map((m) => {
              const value = texts[m.index] ?? m.text ?? ''
              return (
                <div key={m.index} className="flex flex-col gap-2">
                  {m.text !== null ? (
                    <>
                      <textarea value={value} onChange={(e) => setTexts({ ...texts, [m.index]: e.target.value })} rows={Math.min(10, value.split('\n').length + 1)} className={`${inputCls} font-[inherit] leading-relaxed`} />
                      <div className="rounded-2xl rounded-bl-md bg-[#e9eef0] px-3 py-2 text-[13px] whitespace-pre-wrap text-ink/80">{preview(value, vy)}</div>
                    </>
                  ) : (
                    <div className="rounded-xl bg-ink/[0.03] px-3 py-2 text-sm text-ink/60">
                      {m.ai ? `Ответ ИИ по базе знаний на тему: «${m.ai}»` : m.survey ? 'Запускает опрос' : 'Служебное сообщение'}
                    </div>
                  )}
                  {m.buttons.length > 0 && <div className="flex flex-wrap gap-1.5">{m.buttons.map((b) => <span key={b} className="rounded-lg bg-white px-2 py-1 text-xs shadow-sm ring-1 ring-ink/10">{b}</span>)}</div>}
                </div>
              )
            })}
          </div>
          <p className="text-xs text-ink/45">{'{name}'} — имя новичка; {'{ты|вы}'} — вариант для каждой формы обращения, например {'{Хочешь|Хотите}'}. Изменения увидят следующие новички и те, кому шаг ещё не отправлен.</p>
          <div className="flex justify-end gap-2">
            <Btn variant="ghost" onClick={onClose}>Отмена</Btn>
            <Btn onClick={save} disabled={!Object.keys(texts).length || saved}>{saved ? 'Сохранено' : 'Сохранить'}</Btn>
          </div>
        </div>
      )}
    </Modal>
  )
}
