import clsx from 'clsx'
import { X } from 'lucide-react'
import { createContext, useContext, useEffect, type ReactNode } from 'react'

// --- режим демонстрации и роль ------------------------------------------------------------------------

export const DemoContext = createContext({ demo: true })
export const useDemo = () => useContext(DemoContext).demo

// --- базовые блоки -----------------------------------------------------------------------------------

/** Знак Корсика — лиса-корсак на салатовой плашке; вместо эмодзи, которого нет без шрифта эмодзи. */
export function KorsikMark({ className = 'size-10 rounded-2xl' }: { className?: string }) {
  return (
    <div className={clsx('grid shrink-0 place-items-center bg-lime', className)}>
      <img src="/korsik-mark.svg" alt="" className="size-[64%]" />
    </div>
  )
}

export function Card({
  title,
  subtitle,
  action,
  children,
  className,
}: {
  title?: ReactNode
  subtitle?: ReactNode
  action?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section
      className={clsx(
        'rounded-2xl border border-ink/[0.07] bg-white p-5 shadow-[0_1px_3px_rgba(11,46,58,0.05)]',
        className,
      )}
    >
      {(title || action) && (
        <header className="mb-4 flex items-start justify-between gap-3">
          <div>
            <h2 className="text-[15px] font-semibold text-ink">{title}</h2>
            {subtitle && <p className="mt-0.5 text-[13px] text-ink/50">{subtitle}</p>}
          </div>
          {action}
        </header>
      )}
      {children}
    </section>
  )
}

const tones = {
  gray: 'bg-ink/[0.06] text-ink/70',
  green: 'bg-emerald-50 text-emerald-700',
  red: 'bg-rose-50 text-rose-700',
  amber: 'bg-amber-50 text-amber-800',
  blue: 'bg-sky-50 text-sky-800',
  lime: 'bg-lime/25 text-ink',
} as const
export type Tone = keyof typeof tones

export function Badge({ tone = 'gray', children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium whitespace-nowrap',
        tones[tone],
      )}
    >
      {children}
    </span>
  )
}

export function Btn({
  children,
  onClick,
  variant = 'primary',
  disabled,
  type = 'button',
  small,
  demo,
}: {
  children: ReactNode
  onClick?: () => void
  variant?: 'primary' | 'ghost' | 'danger' | 'lime'
  disabled?: boolean
  type?: 'button' | 'submit'
  small?: boolean
  demo?: boolean
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={demo ? 'Инструмент демонстрации' : undefined}
      className={clsx(
        'inline-flex items-center justify-center gap-1.5 rounded-xl font-medium transition active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50',
        small ? 'px-2.5 py-1 text-xs' : 'px-4 py-2 text-sm',
        demo
          ? 'border border-dashed border-sky/60 bg-sky-50 text-sky-800 hover:bg-sky-100'
          : clsx(
              variant === 'primary' && 'bg-ink text-white hover:bg-ink-2',
              variant === 'lime' && 'bg-lime text-ink hover:bg-lime-2',
              variant === 'ghost' && 'bg-ink/[0.05] text-ink hover:bg-ink/10',
              variant === 'danger' && 'bg-rose-600 text-white hover:bg-rose-700',
            ),
      )}
    >
      {children}
    </button>
  )
}

export function Stat({
  label,
  value,
  hint,
  tone,
  details,
}: {
  label: string
  value: ReactNode
  hint?: string
  tone?: 'red' | 'lime'
  details?: string // как посчитан показатель — подсказка при наведении
}) {
  return (
    <div className="rounded-2xl border border-ink/[0.07] bg-white p-4" title={details}>
      <div className="text-[13px] text-ink/55">{label}</div>
      <div
        className={clsx(
          'mt-1 text-3xl font-semibold tracking-tight tabular-nums',
          tone === 'red' && 'text-rose-600',
          tone === 'lime' && 'text-lime-2',
        )}
      >
        {value}
      </div>
      {hint && <div className="mt-1 text-xs text-ink/45">{hint}</div>}
    </div>
  )
}

export function Empty({ children, icon }: { children: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-xl bg-ink/[0.02] px-6 py-8 text-center text-sm text-ink/50">
      {icon}
      {children}
    </div>
  )
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5 text-sm">
      <span className="text-[13px] font-medium text-ink/70">{label}</span>
      {children}
      {hint && <span className="text-xs text-ink/45">{hint}</span>}
    </label>
  )
}

export const inputCls =
  'rounded-xl border border-ink/15 bg-white px-3 py-2.5 text-sm outline-none transition focus:border-ink/40 focus:ring-4 focus:ring-lime/25'

export function Modal({
  open,
  onClose,
  title,
  children,
  wide,
}: {
  open: boolean
  onClose: () => void
  title: string
  children: ReactNode
  wide?: boolean
}) {
  // Закрыть можно тремя способами: крестиком, клавишей Esc и щелчком по затемнённому фону.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])
  if (!open) return null
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 p-4 backdrop-blur-[2px]"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={clsx(
          'relative w-full rounded-3xl bg-white p-6 shadow-2xl',
          wide ? 'max-w-2xl' : 'max-w-lg',
        )}
        onClick={(e) => e.stopPropagation()}
      >
        <button
          type="button"
          onClick={onClose}
          aria-label="Закрыть"
          className="absolute top-4 right-4 grid size-8 place-items-center rounded-full text-ink/40 transition hover:bg-ink/[0.06] hover:text-ink"
        >
          <X size={18} />
        </button>
        <h3 className="mb-5 pr-10 text-lg font-semibold">{title}</h3>
        {children}
      </div>
    </div>
  )
}

export function Tabs<T extends string>({
  value,
  onChange,
  items,
}: {
  value: T
  onChange: (v: T) => void
  items: { id: T; label: string; count?: number; hidden?: boolean }[]
}) {
  return (
    <div className="flex flex-wrap gap-1 rounded-2xl bg-ink/[0.04] p-1">
      {items
        .filter((i) => !i.hidden)
        .map((i) => (
          <button
            key={i.id}
            onClick={() => onChange(i.id)}
            className={clsx(
              'flex items-center gap-1.5 rounded-xl px-3.5 py-2 text-sm transition',
              value === i.id ? 'bg-white font-medium shadow-sm' : 'text-ink/60 hover:text-ink',
            )}
          >
            {i.label}
            {!!i.count && (
              <span className="rounded-full bg-rose-500 px-1.5 text-[11px] font-semibold text-white">
                {i.count}
              </span>
            )}
          </button>
        ))}
    </div>
  )
}

export function Progress({ value, tone = 'lime' }: { value: number; tone?: 'lime' | 'red' }) {
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-ink/[0.07]">
      <div
        className={clsx('h-2 rounded-full transition-all', tone === 'lime' ? 'bg-lime' : 'bg-rose-500')}
        style={{ width: `${Math.max(0, Math.min(100, value))}%` }}
      />
    </div>
  )
}

export function Avatar({ name, size = 40 }: { name: string; size?: number }) {
  const initials = name
    .split(' ')
    .slice(0, 2)
    .map((p) => p[0])
    .join('')
  const hue = [...name].reduce((h, c) => h + c.charCodeAt(0), 0) % 4
  const colors = [
    'bg-lime/40 text-ink',
    'bg-sky-100 text-sky-900',
    'bg-amber-100 text-amber-900',
    'bg-emerald-100 text-emerald-900',
  ]
  return (
    <div
      className={clsx('grid shrink-0 place-items-center rounded-full font-semibold', colors[hue])}
      style={{ width: size, height: size, fontSize: size * 0.36 }}
    >
      {initials}
    </div>
  )
}

// --- словарь интерфейса: внутренние коды → человеческие слова --------------------------------------

export const SITE_NAMES: Record<string, string> = {
  GrPP: 'Офис Группы',
  Luzhniki: 'Офис «Лужники»',
  GTP: 'Завод GTP',
}
export const siteName = (code: string) => SITE_NAMES[code] ?? code

export const TICKET_KIND: Record<string, { label: string; tone: Tone }> = {
  alert: { label: 'Срочно', tone: 'red' },
  question: { label: 'Вопрос', tone: 'blue' },
  problem: { label: 'Проблема', tone: 'amber' },
}

export const SENTIMENT: Record<string, { label: string; tone: Tone }> = {
  negative: { label: 'тревожное', tone: 'red' },
  neutral: { label: 'спокойное', tone: 'gray' },
  positive: { label: 'хорошее', tone: 'green' },
}

export const SEVERITY: Record<string, { label: string; tone: Tone }> = {
  high: { label: 'нужна реакция сегодня', tone: 'red' },
  medium: { label: 'стоит обратить внимание', tone: 'amber' },
  low: { label: 'всё в порядке', tone: 'green' },
}

export function timeAgo(iso: string) {
  const min = Math.round((Date.now() - new Date(iso).getTime()) / 60000)
  if (min < 1) return 'только что'
  if (min < 60) return `${min} мин назад`
  const h = Math.round(min / 60)
  if (h < 24) return `${h} ч назад`
  const d = Math.round(h / 24)
  return d === 1 ? 'вчера' : `${d} дн. назад`
}

export const plural = (n: number, one: string, few: string, many: string) => {
  const m10 = n % 10,
    m100 = n % 100
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? few : many
}
