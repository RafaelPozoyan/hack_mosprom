import { useQuery, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import {
  BellRing,
  Check,
  CheckCircle2,
  ChevronDown,
  Clock,
  Copy,
  Download,
  Ellipsis,
  FileText,
  Hand,
  MessageCircleReply,
  MinusCircle,
  Play,
  QrCode,
  Upload,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import {
  ApiError,
  PHASES,
  api,
  fmtDate,
  fmtDateTime,
  probationDay,
  withToken,
  type AnalysisItem,
  type Employee,
  type EmployeeCardData,
  type Meta,
  type Notification,
  type SupportReason,
  type Ticket,
  type TimelineStep,
} from '../api'
import ChatPhone from '../components/ChatPhone'
import SupportNote, { useSupport } from '../components/SupportNote'
import TeamPicker from '../components/TeamPicker'
import {
  Avatar,
  Badge,
  Btn,
  Card,
  Empty,
  Progress,
  TICKET_KIND,
  Tabs,
  plural,
  siteName,
  timeAgo,
  useDemo,
} from '../components/ui'
import { InviteModal } from './Employees'

type Tab = 'journey' | 'surveys' | 'signals' | 'tickets'

export default function EmployeeCard() {
  const { id } = useParams()
  const qc = useQueryClient()
  const demo = useDemo()
  const [tab, setTab] = useState<Tab>('journey')
  const [invite, setInvite] = useState(false)
  const { data, error, refetch, isFetching } = useQuery({
    queryKey: ['employee', id],
    queryFn: () => api.get<EmployeeCardData>(`/api/employees/${id}`),
    refetchInterval: 10_000,
  })
  const { data: support } = useSupport(Number(id))
  const refresh = useCallback(() => qc.invalidateQueries({ queryKey: ['employee', id] }), [qc, id])
  if (!data) return error ? <LoadError error={error} retry={() => refetch()} busy={isFetching} /> : null
  const e = data.employee
  const { day, total } = probationDay(e)
  const concerns = concernsOf(data, support?.reasons ?? [])
  const openTickets = data.tickets.filter((t) => t.status === 'open').length

  return (
    <div className={clsx('mx-auto flex gap-6', demo ? 'max-w-[1500px]' : 'max-w-5xl')}>
      <div className="flex min-w-0 flex-1 flex-col gap-5">
        <Card>
          <div className="flex items-center gap-4">
            <Avatar name={e.full_name} size={56} />
            <div className="min-w-0 flex-1">
              <h1 className="text-xl font-semibold tracking-tight">{e.full_name}</h1>
              <div className="text-sm text-ink/55">
                {e.position || 'Должность не указана'} · {siteName(e.site_code)}
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-sm text-ink/55">
                <TeamPicker e={e} onDone={refresh} />
                {e.address_form && (
                  <span title="Как Корсик обращается к новичку — новичок выбрал сам">
                    · на «{e.address_form === 'vy' ? 'вы' : 'ты'}»
                  </span>
                )}
                {e.in_dialog_with_hr && <Badge tone="blue">сейчас в диалоге с HR</Badge>}
              </div>
            </div>
            {/* у приглашённого QR-код — в блоке «Нужно ваше действие»; остальным он нужен редко */}
            {e.status !== 'invited' && <MoreMenu onInvite={() => setInvite(true)} />}
          </div>
          <SupportNote empId={e.id} name={e.full_name} onSent={refresh} />
          <div className="mt-5">
            {day < 1 ? (
              <div className="text-sm text-ink/60">
                {1 - day === 1
                  ? 'Выходит на работу завтра.'
                  : `Выходит на работу через ${1 - day} ${plural(1 - day, 'день', 'дня', 'дней')}.`}
                {e.status !== 'invited' && ' Корсик уже на связи и помогает подготовиться.'}
              </div>
            ) : (
              <>
                <div className="mb-1.5 flex justify-between text-sm">
                  <span className="text-ink/60">Испытательный срок</span>
                  <span className="font-medium tabular-nums">
                    день {Math.min(day, total)} из {total}
                  </span>
                </div>
                <Progress value={(100 * day) / total} />
              </>
            )}
          </div>
        </Card>

        <Actions data={data} onDone={refresh} />

        <Tabs
          value={tab}
          onChange={setTab}
          items={[
            { id: 'journey', label: 'Путь адаптации' },
            { id: 'surveys', label: 'Опросы' },
            { id: 'signals', label: 'Что беспокоит', count: concerns.items.length },
            { id: 'tickets', label: 'Обращения', count: openTickets },
          ]}
        />

        {tab === 'journey' && (
          <Journey steps={data.timeline} empId={e.id} active={e.status === 'active'} onDone={refresh} />
        )}
        {tab === 'surveys' && <Surveys data={data} />}
        {tab === 'signals' && <Signals data={data} {...concerns} />}
        {tab === 'tickets' && <TicketsTab data={data} />}
      </div>

      {demo && (
        <div className="hidden shrink-0 xl:block">
          <div className="sticky top-6">
            <div className="mb-2 text-center text-xs font-medium text-sky-800">
              Так {e.first_name} видит Корсика в MAX
            </div>
            <ChatPhone employee={e} onActivity={refresh} />
          </div>
        </div>
      )}
      {invite && <InviteModal e={e} onClose={() => setInvite(false)} />}
    </div>
  )
}

/** Карточка не загрузилась: сотрудника нет (удалили, неверная ссылка) или нет связи с сервером. */
function LoadError({ error, retry, busy }: { error: Error; retry: () => void; busy: boolean }) {
  const missing = error instanceof ApiError && [404, 422].includes(error.status)
  return (
    <Card className="mx-auto w-full max-w-5xl">
      {missing ? (
        <Empty>
          Такого сотрудника нет — возможно, карточку удалили
          <Link to="/employees" className="font-medium text-ink/70 hover:text-ink">
            К списку сотрудников
          </Link>
        </Empty>
      ) : (
        <Empty>
          Не удалось загрузить карточку. Проверьте соединение.
          <Btn small variant="ghost" onClick={retry} disabled={busy}>
            Повторить
          </Btn>
        </Empty>
      )}
    </Card>
  )
}

/** Редкие действия в шапке: сейчас одно — снова показать QR-код и ссылку-приглашение. */
function MoreMenu({ onInvite }: { onInvite: () => void }) {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const outside = (ev: MouseEvent) => !box.current?.contains(ev.target as Node) && setOpen(false)
    const esc = (ev: KeyboardEvent) => ev.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', outside)
    document.addEventListener('keydown', esc)
    return () => {
      document.removeEventListener('mousedown', outside)
      document.removeEventListener('keydown', esc)
    }
  }, [open])
  return (
    <div ref={box} className="relative self-start">
      <button
        aria-label="Ещё действия"
        aria-expanded={open}
        title="Ещё действия"
        onClick={() => setOpen(!open)}
        className="grid size-9 place-items-center rounded-xl text-ink/45 transition hover:bg-ink/[0.05] hover:text-ink"
      >
        <Ellipsis size={18} />
      </button>
      {open && (
        <div className="absolute right-0 z-10 mt-1 w-60 rounded-xl border border-ink/10 bg-white p-1 shadow-lg">
          <button
            onClick={() => {
              setOpen(false)
              onInvite()
            }}
            className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm hover:bg-ink/[0.04]"
          >
            <QrCode size={16} className="text-ink/50" /> QR-приглашение
          </button>
        </div>
      )}
    </div>
  )
}

// --- нужно ваше действие -----------------------------------------------------------------------------

/** Плановое время шага ещё не пришло. */
const early = (t: TimelineStep) => !!t.run_at && Date.parse(t.run_at) > Date.now()

/** Шаг «по вашей команде», время которого пришло: его кнопки — в блоке «Нужно ваше действие», а не в ленте. */
const awaitsHr = (t: TimelineStep) => t.manual && t.status === 'manual' && !early(t)

/** Тема обращения из опроса приходит как «вопрос → ответ»; HR важнее слова самого новичка. */
const ownWords = (subject: string) => subject.slice(subject.lastIndexOf('→') + 1).trim()

/** Только то, что HR может сделать сам: ответить на обращение, отправить шаг по команде, пригласить новичка.
 *  Заметная кнопка — у первой строки, у остальных — спокойные: главное действие на экране одно. */
function Actions({ data, onDone }: { data: EmployeeCardData; onDone: () => void }) {
  const e = data.employee
  const rows = [
    ...data.tickets
      .filter((t) => t.status === 'open')
      .map((t) => (main: boolean) => <TicketAction key={`t${t.id}`} t={t} name={e.first_name} main={main} />),
    ...(e.status === 'active' ? data.timeline.filter(awaitsHr) : []).map((t) => (main: boolean) => (
      <StepAction key={t.step_id} t={t} empId={e.id} onDone={onDone} main={main} />
    )),
    ...(e.status === 'invited' ? [(main: boolean) => <InviteAction key="invite" e={e} main={main} />] : []),
  ]
  if (!rows.length) return null
  return (
    <Card title="Нужно ваше действие">
      <div className="flex flex-col divide-y divide-ink/[0.06]">{rows.map((row, i) => row(i === 0))}</div>
    </Card>
  )
}

function ActionRow({
  icon,
  title,
  children,
  actions,
  error,
}: {
  icon: ReactNode
  title: ReactNode
  children: ReactNode
  actions: ReactNode
  error?: string
}) {
  return (
    <div className="flex items-center gap-4 py-3 first:pt-0 last:pb-0">
      {icon}
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-4 gap-y-3">
        <div className="min-w-0 flex-1 basis-64">
          <div className="text-sm font-medium">{title}</div>
          <div className="mt-0.5 text-sm text-ink/55">{children}</div>
          {error && (
            <div role="alert" className="mt-1.5 text-sm text-rose-600">
              {error}
            </div>
          )}
        </div>
        <div className="flex flex-wrap gap-2">{actions}</div>
      </div>
    </div>
  )
}

const rowIcon = (icon: ReactNode) => (
  <div className="grid size-10 shrink-0 place-items-center rounded-xl bg-amber-50 text-amber-700">{icon}</div>
)

function TicketAction({ t, name, main }: { t: Ticket; name: string; main: boolean }) {
  const nav = useNavigate()
  return (
    <ActionRow
      icon={rowIcon(<MessageCircleReply size={19} />)}
      title={`${name} ждёт ответа HR с ${fmtDate(t.created_at)}`}
      actions={
        <Btn variant={main ? 'primary' : 'ghost'} onClick={() => nav(`/tickets?id=${t.id}`)}>
          Ответить
        </Btn>
      }
    >
      <span className="line-clamp-2">«{ownWords(t.subject)}»</span>
    </ActionRow>
  )
}

function StepAction({
  t,
  empId,
  onDone,
  main,
}: {
  t: TimelineStep
  empId: number
  onDone: () => void
  main: boolean
}) {
  const { run, busy, error } = useRunStep(empId, t, onDone)
  const plan = t.step_id === 'd3_plan'
  const variant = main ? 'primary' : 'ghost'
  return (
    <ActionRow
      icon={rowIcon(plan ? <FileText size={19} /> : <Hand size={19} />)}
      title={`${t.title} — ${t.run_at ? `ждёт с ${fmtDate(t.run_at)}` : 'ждёт вашей команды'}`}
      error={error}
      actions={
        plan ? (
          <>
            <AttachPlan run={run} busy={busy} variant={variant} />
            <Btn variant="ghost" onClick={() => run()} disabled={busy}>
              Отправить без плана
            </Btn>
          </>
        ) : (
          <Btn variant={variant} onClick={() => run()} disabled={busy}>
            Отправить
          </Btn>
        )
      }
    >
      {plan
        ? 'Корсик отправит новичку файл от руководителя вместе с сообщением о плане.'
        : 'Этот шаг Корсик отправляет по вашей команде.'}
    </ActionRow>
  )
}

function InviteAction({ e, main }: { e: Employee; main: boolean }) {
  const [copied, setCopied] = useState(false)
  const qr = withToken(`/api/employees/${e.id}/qr.svg`)
  return (
    <ActionRow
      icon={
        <img
          src={qr}
          alt="QR-код приглашения"
          className="size-28 shrink-0 rounded-xl border border-ink/10 p-1.5"
        />
      }
      title="Приглашение в MAX ещё не открыто"
      actions={
        <>
          {e.invite_link && (
            <Btn
              variant={main ? 'primary' : 'ghost'}
              onClick={() => navigator.clipboard.writeText(e.invite_link!).then(() => setCopied(true))}
            >
              {copied ? <Check size={15} /> : <Copy size={15} />}{' '}
              {copied ? 'Скопировано' : 'Скопировать ссылку'}
            </Btn>
          )}
          <a href={qr} download={`QR ${e.full_name}.svg`}>
            <Btn variant="ghost">
              <Download size={15} /> Скачать QR
            </Btn>
          </a>
        </>
      }
    >
      Отправьте ссылку или добавьте QR-код в оффер. Корсик начнёт путь адаптации, как только новичок откроет
      приглашение.
    </ActionRow>
  )
}

/** Отправить шаг сейчас — по команде HR или перемоткой в демо. Отказы сервера — человеческим языком. */
function useRunStep(empId: number, t: TimelineStep, onDone: () => void) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    setError('')
  }, [t.status])
  const run = async (f?: File) => {
    setBusy(true)
    const fd = new FormData()
    if (f) fd.append('file', f)
    setError('')
    try {
      await api.post(`/api/employees/${empId}/steps/${t.step_id}/run`, fd)
      onDone()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setError(e.detail ?? 'Шаг сейчас недоступен. Обновите карточку и проверьте его состояние.')
        onDone()
      } else if (e instanceof ApiError && e.status === 503) {
        setError('Отправка не завершена. Корсик повторит её автоматически. Обновите карточку позже.')
      } else {
        setError('Не удалось подтвердить отправку. Обновите карточку и проверьте сообщения перед повтором.')
      }
    } finally {
      setBusy(false)
    }
  }
  return { run, busy, error }
}

/** «Приложить план»: выбранный файл Корсик отправит новичку вместе с сообщениями шага. */
function AttachPlan({
  run,
  busy,
  variant,
  small,
}: {
  run: (f: File) => void
  busy: boolean
  variant: 'primary' | 'ghost'
  small?: boolean
}) {
  const file = useRef<HTMLInputElement>(null)
  return (
    <>
      <input
        ref={file}
        type="file"
        hidden
        onChange={(ev) => {
          const f = ev.target.files?.[0]
          ev.target.value = '' // после ошибки тот же файл можно выбрать снова
          if (f) run(f)
        }}
      />
      <Btn small={small} variant={variant} onClick={() => file.current?.click()} disabled={busy}>
        <Upload size={small ? 13 : 15} /> Приложить план
      </Btn>
    </>
  )
}

// --- путь адаптации ----------------------------------------------------------------------------------

// Значок и подпись статуса шага. Подпись серая, если не сказано иначе; date — показывать ли рядом
// плановое время (у отправленных и пропущенных шагов оно уже ничего не говорит).
type StepStatus = { label: string; icon: typeof Clock; cls: string; text?: string; date?: boolean }
const WAIT = { icon: Clock, cls: 'text-amber-600', text: 'text-amber-700' } // ждём новичка или его ответа
const STATUS: Record<string, StepStatus> = {
  sent: { label: 'отправлено', icon: CheckCircle2, cls: 'text-lime-2' },
  done: { label: 'отправлено', icon: CheckCircle2, cls: 'text-lime-2' },
  pending: { label: 'запланировано', icon: Clock, cls: 'text-ink/35', date: true },
  running: { label: 'отправляется', icon: Clock, cls: 'text-sky' },
  waiting: { label: 'ждём обязательный ответ', ...WAIT },
  await_start: { label: 'приглашение ещё не открыто', ...WAIT },
  blocked: { label: 'после обязательного ответа', ...WAIT, date: true },
  manual: { label: 'по вашей команде', icon: Hand, cls: 'text-amber-600', date: true },
  failed: {
    label: 'не удалось отправить',
    icon: MinusCircle,
    cls: 'text-rose-500',
    text: 'text-rose-600',
    date: true,
  },
  skipped: { label: 'пропущено', icon: MinusCircle, cls: 'text-ink/25' },
  cancelled: { label: 'пропущено', icon: MinusCircle, cls: 'text-ink/25' },
}

/** Шаг уже позади: отправлен или пропущен. Такие шаги свёрнуты, открыты текущий и следующие. */
const passed = (t: TimelineStep) => ['sent', 'done', 'skipped', 'cancelled'].includes(t.status)
const skipped = (t: TimelineStep) => t.status === 'skipped' || t.status === 'cancelled'

const byPhase = (steps: TimelineStep[]) =>
  PHASES.map((p) => ({ phase: p, items: steps.filter((s) => s.phase === p) })).filter((g) => g.items.length)

function Journey({
  steps,
  empId,
  active,
  onDone,
}: {
  steps: TimelineStep[]
  empId: number
  active: boolean
  onDone: () => void
}) {
  const done = steps.filter(passed)
  const missed = done.filter(skipped).length
  const ahead = byPhase(steps.filter((s) => !passed(s)))
  const later = ahead.slice(2).flatMap((g) => g.items) // открыты текущий этап и следующий, дальние — свёрнуты
  const last = later.at(-1)?.run_at
  const waiting = steps.find((s) => s.status === 'waiting')
  const step = (t: TimelineStep) => (
    <Step key={t.step_id} t={t} empId={empId} active={active} onDone={onDone} />
  )
  return (
    <div className="flex flex-col gap-4">
      {waiting && (
        <Card title="Ждём обязательный ответ">
          <p className="text-sm text-ink/70">
            На шаг «{waiting.title}» пока нет полного ответа. Следующие опросы продолжатся после него.
            Справочная информация и помощь HR остаются доступны.
          </p>
        </Card>
      )}
      {done.length > 0 && (
        <Fold
          icon={<CheckCircle2 size={18} className="shrink-0 text-lime-2" />}
          title={`Пройдено ${done.length} из ${steps.length}`}
          note={missed > 0 && `(${missed} ${plural(missed, 'пропущен', 'пропущено', 'пропущено')})`}
          steps={done}
          step={step}
        />
      )}
      {ahead.slice(0, 2).map((g) => (
        <Card key={g.phase} title={g.phase}>
          <ol className="flex flex-col">{g.items.map(step)}</ol>
        </Card>
      ))}
      {later.length > 0 && (
        <Fold
          icon={<Clock size={18} className="shrink-0 text-ink/35" />}
          title={`Дальше по плану: ${later.length} ${plural(later.length, 'шаг', 'шага', 'шагов')}`}
          note={last && `до ${fmtDate(last)}`}
          steps={later}
          step={step}
        />
      )}
    </div>
  )
}

/** Свёрнутая часть ленты: одна строка-итог, по нажатию — шаги по этапам. */
function Fold({
  icon,
  title,
  note,
  steps,
  step,
}: {
  icon: ReactNode
  title: string
  note: ReactNode
  steps: TimelineStep[]
  step: (t: TimelineStep) => ReactNode
}) {
  return (
    <Card>
      <details className="group">
        <summary className="flex cursor-pointer list-none items-center gap-3 text-[15px] font-semibold select-none">
          {icon}
          <span>
            {title} {note && <span className="font-normal text-ink/50">{note}</span>}
          </span>
          <ChevronDown size={18} className="ml-auto text-ink/40 transition group-open:rotate-180" />
        </summary>
        <div className="mt-4 flex flex-col gap-4">
          {byPhase(steps).map((g) => (
            <div key={g.phase}>
              <h3 className="text-[13px] font-semibold text-ink/50">{g.phase}</h3>
              <ol className="flex flex-col">{g.items.map(step)}</ol>
            </div>
          ))}
        </div>
      </details>
    </Card>
  )
}

/** Строка шага. Кнопки, которые сейчас не нужны, видны при наведении: демо-перемотка и отправка шага
 *  «по вашей команде» раньше срока. Шаг, которого HR ждёт сейчас, отправляют из блока «Нужно ваше действие». */
function Step({
  t,
  empId,
  active,
  onDone,
}: {
  t: TimelineStep
  empId: number
  active: boolean
  onDone: () => void
}) {
  const demo = useDemo()
  const { run, busy, error } = useRunStep(empId, t, onDone)
  const st = STATUS[t.status] ?? STATUS.pending
  const Icon = st.icon
  const hr = awaitsHr(t)
  const onHover = 'opacity-0 transition group-hover:opacity-100 focus-within:opacity-100'
  return (
    <li className="group flex items-start gap-3 border-b border-ink/[0.05] py-2.5 last:border-0">
      <Icon size={18} className={clsx('mt-0.5 shrink-0', st.cls)} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className={clsx('font-medium', passed(t) && 'text-ink/60')}>{t.title}</span>
          {t.audience === 'manager' && <Badge tone="blue">руководителю</Badge>}
          {t.audience === 'recruiter' && <Badge tone="blue">рекрутеру</Badge>}
        </div>
        <div className="mt-0.5 line-clamp-1 text-xs text-ink/45">{t.preview}</div>
        {t.status_note && <div className="mt-1 text-xs text-ink/60">{t.status_note}</div>}
        {error && (
          <div role="alert" className="mt-2 text-sm text-rose-600">
            {error}
          </div>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-3">
        {active && t.manual && !passed(t) && !hr && (
          <span className={clsx('flex gap-2', early(t) && onHover)}>
            {t.step_id === 'd3_plan' && <AttachPlan small run={run} busy={busy} variant="ghost" />}
            <Btn small onClick={() => run()} disabled={busy}>
              Отправить
            </Btn>
          </span>
        )}
        {demo && !t.manual && !passed(t) && (
          <span className={onHover}>
            <Btn small demo onClick={() => run()} disabled={busy}>
              <Play size={12} /> {t.status === 'waiting' ? 'Продолжить' : 'сейчас'}
            </Btn>
          </span>
        )}
        <div className="text-right text-xs">
          <div className={hr ? WAIT.text : (st.text ?? 'text-ink/50')}>
            {hr ? 'ждёт вашей команды' : st.label}
          </div>
          {st.date && t.run_at && (
            <div className="mt-0.5 text-ink/40 tabular-nums">{fmtDateTime(t.run_at)}</div>
          )}
        </div>
      </div>
    </li>
  )
}

// --- опросы, сигналы, обращения ----------------------------------------------------------------------

function Surveys({ data }: { data: EmployeeCardData }) {
  if (!data.surveys.length)
    return (
      <Card>
        <Empty>Опросов пока не было. Первый — по итогам третьего рабочего дня.</Empty>
      </Card>
    )
  return (
    <div className="flex flex-col gap-4">
      {data.surveys.map((g) => (
        <Card key={g.title} title={g.title} subtitle={timeAgo(g.at)}>
          <div className="flex flex-col divide-y divide-ink/[0.05]">
            {g.items.map((i, n) => (
              <div key={n} className="py-2.5">
                <div className="flex items-start justify-between gap-4 text-sm">
                  <span className="text-ink/70">{i.question}</span>
                  <span className={clsx('shrink-0 text-right font-medium', i.concern && 'text-rose-600')}>
                    {i.answer}
                  </span>
                </div>
                {i.comment && (
                  <div className="mt-1.5 rounded-lg bg-ink/[0.03] px-3 py-2 text-sm text-ink/75">
                    «{i.comment}»
                  </div>
                )}
              </div>
            ))}
          </div>
        </Card>
      ))}
    </div>
  )
}

/** Тревожный сигнал: ответ в опросе с флагом или сообщение, которое Корсик при разборе счёл тревожным.
 *  Те же сигналы, что сервер показывает в шапке карточки и в списке сотрудников (metrics.risk_signals). */
type Concern = {
  flags: string[]
  question?: string
  text: string
  summary?: string
  urgent: boolean
  source: string
  at: string
}

/** Срочные первыми, затем свежие. Срочность — как в шапке карточки: высокая важность при разборе или причина,
 *  которую сервер пометил срочной (политика analytics.high в сценарии). Ответ опроса, который Корсик разобрал как
 *  тревожный, показываем один раз — разбором с выводом; спокойные разборы уходят под «Ещё N спокойных». */
function concernsOf(data: EmployeeCardData, reasons: SupportReason[]) {
  const urgent = new Set(reasons.filter((r) => r.urgent).map((r) => r.reason))
  const tense = data.analyses.filter((a) => a.severity !== 'low')
  const covered = new Set(tense.map((a) => a.text))
  const answers = data.surveys.flatMap((g) =>
    g.items
      .filter((i) => i.flag && !(i.comment && covered.has(i.comment)))
      .map((i) => ({
        flags: [i.flag!],
        question: i.question.replace(/^(Вопрос\s*\d+\s*[:.]|\d+\s*[.)])\s*/, ''), // без «Вопрос 3:» и «1)»
        text: i.comment || i.answer,
        urgent: urgent.has(i.flag!),
        source: 'из опроса',
        at: g.at,
      })),
  )
  const messages = tense.map((a) => ({
    flags: a.risk_flags,
    text: a.text,
    summary: a.summary,
    urgent: a.severity === 'high' || a.risk_flags.some((f) => urgent.has(f)),
    source: a.source === 'survey' ? 'из опроса' : 'из сообщения',
    at: a.at,
  }))
  const shown = new Set(answers.map((c) => c.text))
  const items: Concern[] = [...messages, ...answers].sort(
    (a, b) => Number(b.urgent) - Number(a.urgent) || Date.parse(b.at) - Date.parse(a.at),
  )
  return { items, calm: data.analyses.filter((a) => a.severity === 'low' && !shown.has(a.text)) }
}

function Signals({ data, items, calm }: { data: EmployeeCardData; items: Concern[]; calm: AnalysisItem[] }) {
  return (
    <div className="flex flex-col gap-4">
      <Card
        title="Что заметил Корсик"
        subtitle="Тревожные ответы в опросах и сообщения новичка, важное — первым"
      >
        {items.length === 0 ? (
          <Empty>
            Ничего тревожного
            {calm.length === 0 && (
              <span className="text-xs text-ink/40">
                Если {data.employee.first_name} ответит тревожно в опросе или в чате, это появится здесь
              </span>
            )}
          </Empty>
        ) : (
          <div className="flex flex-col gap-3">
            {items.map((c, i) => (
              <div
                key={i}
                className={clsx(
                  'rounded-2xl border p-4',
                  c.urgent ? 'border-rose-200 bg-rose-50/40' : 'border-ink/[0.07]',
                )}
              >
                {c.flags.length > 0 && (
                  <div className="mb-2 flex flex-wrap gap-1.5">
                    {c.flags.map((f) => (
                      <Badge key={f} tone={c.urgent ? 'red' : 'amber'}>
                        {f}
                      </Badge>
                    ))}
                  </div>
                )}
                {c.question && <div className="mb-1 text-xs text-ink/50">{c.question}</div>}
                <div className="text-sm">«{c.text}»</div>
                {c.summary && <div className="mt-2 text-sm text-ink/60">{c.summary}</div>}
                <div className="mt-2 flex justify-between gap-3 text-xs text-ink/40">
                  <span className="font-medium text-rose-700">{c.urgent && 'срочно'}</span>
                  <span>
                    {c.source} · {timeAgo(c.at)}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
        {calm.length > 0 && (
          <details className="group mt-3">
            <summary className="flex w-fit cursor-pointer list-none items-center gap-1 text-[13px] text-ink/55 select-none hover:text-ink/80">
              {items.length ? 'Ещё' : 'Показать'} {calm.length}{' '}
              {plural(calm.length, 'спокойное', 'спокойных', 'спокойных')}
              <ChevronDown size={14} className="transition group-open:rotate-180" />
            </summary>
            <div className="mt-1 flex flex-col divide-y divide-ink/[0.05]">
              {calm.map((a, i) => (
                <div key={i} className="flex items-start justify-between gap-4 py-2.5 text-sm">
                  <span className="text-ink/70">«{a.text}»</span>
                  <span className="shrink-0 text-xs text-ink/40">
                    {a.source === 'survey' ? 'из опроса' : 'из сообщения'} · {timeAgo(a.at)}
                  </span>
                </div>
              ))}
            </div>
          </details>
        )}
      </Card>
      <Notifications data={data} />
    </div>
  )
}

/** Одно и то же уведомление тому же человеку показываем один раз — самое свежее (сервер отдаёт свежие первыми). */
const unique = (list: Notification[]) =>
  list.filter((n, i) => list.findIndex((m) => m.to === n.to && m.text === n.text) === i)

/** Почему уведомление не дошло в MAX. Сервер помнит только «дошло или нет» (channels.notify_staff): не дойти
 *  могло, если Корсик не подключён к MAX, адресат не назначен или не подключил MAX, либо MAX не принял сообщение.
 *  Причину видно по нынешнему состоянию команды; если сейчас всё подключено — причину не называем. */
function delivery(n: Notification, e: Employee, meta: Meta | undefined) {
  if (n.delivered) return 'доставлено в MAX'
  const who = n.to === 'manager' ? 'руководитель' : 'рекрутер'
  const id = n.to === 'manager' ? e.manager_id : e.recruiter_id
  if (meta && !meta.max_connected) return 'в MAX не доставлено — Корсик не подключён к MAX'
  if (!id) return `в MAX не доставлено — ${who} не назначен`
  if (meta?.staff.find((x) => x.id === id)?.max_connected === false)
    return `в MAX не доставлено — ${who} не подключил MAX`
  return 'в MAX не доставлено'
}

function Notifications({ data }: { data: EmployeeCardData }) {
  const { data: meta } = useQuery({ queryKey: ['meta'], queryFn: () => api.get<Meta>('/api/meta') })
  const list = unique(data.notifications)
  return (
    <Card title="Кого Корсик уведомил">
      {list.length === 0 ? (
        <Empty>Корсик пока не писал руководителю и рекрутеру об этом новичке</Empty>
      ) : (
        <div className="flex flex-col gap-2">
          {list.map((n, i) => (
            <div key={i} className="flex gap-3 rounded-xl bg-sky-50/60 p-3 text-sm">
              <BellRing size={16} className="mt-0.5 shrink-0 text-sky-700" />
              <div className="min-w-0 flex-1">
                <div className="mb-0.5 text-xs text-ink/50">
                  {n.to === 'manager' ? 'Руководителю' : 'Рекрутеру'} · {timeAgo(n.at)} ·{' '}
                  <span className={clsx(!n.delivered && 'text-amber-700')}>
                    {delivery(n, data.employee, meta)}
                  </span>
                </div>
                <div className="whitespace-pre-wrap text-ink/80">{n.text}</div>
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

function TicketsTab({ data }: { data: EmployeeCardData }) {
  if (!data.tickets.length)
    return (
      <Card>
        <Empty>Если {data.employee.first_name} попросит помощи HR в чате, обращение появится здесь</Empty>
      </Card>
    )
  return (
    <Card>
      <div className="flex flex-col divide-y divide-ink/[0.05]">
        {data.tickets.map((t) => (
          <Link
            key={t.id}
            to={`/tickets?id=${t.id}`}
            className="-mx-2 flex items-center gap-3 rounded-xl px-2 py-3 hover:bg-ink/[0.025]"
          >
            <Badge tone={TICKET_KIND[t.kind]?.tone}>{TICKET_KIND[t.kind]?.label}</Badge>
            <span className="line-clamp-2 min-w-0 flex-1 text-sm">{ownWords(t.subject)}</span>
            <span className="text-xs text-ink/45">
              {t.status === 'open' ? 'открыто' : 'решено'} · {timeAgo(t.created_at)}
            </span>
          </Link>
        ))}
      </div>
    </Card>
  )
}
