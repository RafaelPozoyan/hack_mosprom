export type Button = { text: string; payload: string | null; url: string | null }
export type ChatMessage = {
  id: number
  direction: 'in' | 'out'
  author: 'employee' | 'bot' | 'hr'
  text: string
  payload: {
    buttons?: Button[][]
    image?: string | null
    file?: string | null
    sources?: { title: string; heading: string; quote: string }[]
    button?: boolean
  }
  step_id: string | null
  created_at: string
}

export type Employee = {
  id: number
  full_name: string
  first_name: string
  position: string
  site_code: string
  start_date: string
  probation_end: string
  manager: string | null
  manager_id: number | null
  recruiter: string | null
  address_form: 'ty' | 'vy' | null
  channel: 'web' | 'max'
  status: 'invited' | 'active' | 'finished'
  stage: string
  invite_link: string | null
  survey: { id: string; q: number } | null
  in_dialog_with_hr: boolean
  signals?: number
  open_ticket?: boolean
}

export type Signal = { flag: string; source: string; text: string; severity?: string; at: string }

export type Dashboard = {
  attention: {
    id: number
    kind: string
    severity: string
    employee: string
    employee_id: number
    subject: string
    created_at: string
  }[]
  kpi: {
    newcomers: number
    questions: number
    ai_resolved_pct: number | null
    helpful_pct: number | null
    open_tickets: number
    red_flags: number
  }
  stages: { stage: string; count: number }[]
  sentiment_weekly: { week: string; positive: number; neutral: number; negative: number }[]
  question_topics: { topic: string; count: number }[]
  knowledge_gaps: { question: string; count: number }[]
  risk: { employee_id: number; name: string; site: string; stage: string; signals: Signal[]; high: boolean }[]
  scores: { topic: string; avg: number; n: number }[]
}

export type Ticket = {
  id: number
  kind: 'question' | 'problem' | 'alert'
  status: 'open' | 'closed'
  severity: string
  subject: string
  summary: string
  employee_id: number
  employee: string
  site: string
  created_at: string
  response?: TicketResponse
  messages?: ChatMessage[]
}

/** Срок первого ответа HR: ответственный рекрутер, срок в рабочих часах площадки и напоминания. */
export type TicketResponse = {
  recruiter: string | null
  due_at: string
  first_reply_at: string | null
  overdue: boolean
  reminded_at: string | null
  reminder_delivered: boolean | null
}

export type ResponseTime = {
  rules: { hours: number; urgent_hours: number }
  first_reply: { median_min: number | null; tickets: number; days: number }
  overdue: (Ticket & { response: TicketResponse })[]
}

export type Meta = {
  sites: { code: string; name: string; kind: string; tz: string }[]
  staff: {
    id: number
    name: string
    role: string
    max_connected: boolean
    invite_link: string | null
    code: string | null
  }[]
  steps: {
    id: string
    title: string
    phase: string
    at: Record<string, string | number | boolean> | null
    sites: string[]
    manual: boolean
    audience: string
    preview: string
  }[]
  surveys: Record<string, { title: string; questions: number }>
  max_connected: boolean
  max_bot_name: string
  llm_model: string
}

const token = () => sessionStorage.getItem('korsik_token') ?? localStorage.getItem('korsik_token') ?? ''
export const AUTH_EXPIRED = 'korsik:auth-expired'

export function clearToken() {
  sessionStorage.removeItem('korsik_token')
  localStorage.removeItem('korsik_token')
}

export function saveToken(value: string) {
  clearToken()
  sessionStorage.setItem('korsik_token', value)
}

export class ApiError extends Error {
  readonly status: number
  readonly detail: string | null

  constructor(status: number, body: string) {
    super(`${status}: ${body}`)
    this.status = status
    this.detail = null
    try {
      const data: unknown = JSON.parse(body)
      if (data && typeof data === 'object' && 'detail' in data && typeof data.detail === 'string') {
        this.detail = data.detail
      }
    } catch {
      // Прокси может вернуть HTML вместо JSON; в интерфейсе остаётся запасной текст.
    }
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { ...(init?.headers as Record<string, string>) }
  const credential = token()
  if (credential) headers.Authorization = `Bearer ${credential}`
  if (init?.body && !(init.body instanceof FormData)) headers['Content-Type'] = 'application/json'
  const r = await fetch(path, { ...init, headers })
  if (!r.ok) {
    if (r.status === 401 && credential === token()) window.dispatchEvent(new Event(AUTH_EXPIRED))
    throw new ApiError(r.status, await r.text())
  }
  return r.json()
}

export async function checkAccess(credential = token()): Promise<{ required: boolean }> {
  const headers: Record<string, string> = credential ? { Authorization: `Bearer ${credential}` } : {}
  const response = await fetch('/api/auth', { headers })
  if (!response.ok) throw new ApiError(response.status, await response.text())
  return response.json()
}

export const api = {
  get: <T>(path: string) => req<T>(path),
  post: <T>(path: string, body?: unknown) =>
    req<T>(path, { method: 'POST', body: body instanceof FormData ? body : JSON.stringify(body ?? {}) }),
  patch: <T>(path: string, body: unknown) => req<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) => req<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  del: <T>(path: string) => req<T>(path, { method: 'DELETE' }),
  stream: (path: string) => new EventSource(withToken(path)),
}

export const withToken = (path: string) => (token() ? `${path}?token=${token()}` : path)

// Время для людей — всегда по Москве, а не по часам браузера (у HR в другом поясе или в контейнере записи демо — иначе)
export const fmtDate = (iso: string) =>
  new Date(iso).toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', timeZone: 'Europe/Moscow' })
export const fmtTime = (iso: string) =>
  new Date(iso).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow' })
export const fmtDateTime = (iso: string) =>
  new Date(iso).toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    timeZone: 'Europe/Moscow',
  })

export type TimelineStep = {
  step_id: string
  title: string
  phase: string
  run_at: string | null
  manual: boolean
  audience: string
  status: string
  status_note: string | null
  preview: string
}
export type SurveyGroup = {
  title: string
  at: string
  items: { question: string; answer: string; comment: string | null; concern: boolean; flag: string | null }[]
}
export type AnalysisItem = {
  text: string
  sentiment: string
  emotions: string[]
  topics: string[]
  risk_flags: string[]
  severity: string
  summary: string
  source: string
  at: string
}
export type Notification = { to?: string; text: string; delivered: boolean; at: string }
export type EmployeeCardData = {
  employee: Employee
  timeline: TimelineStep[]
  surveys: SurveyGroup[]
  analyses: AnalysisItem[]
  tickets: Ticket[]
  notifications: Notification[]
}
/** Сотрудник глазами руководителя: сервер отдаёт только эти поля (MANAGER_FIELDS в admin.py). */
export type ManagerEmployee = Pick<
  Employee,
  | 'id'
  | 'full_name'
  | 'first_name'
  | 'position'
  | 'site_code'
  | 'start_date'
  | 'probation_end'
  | 'status'
  | 'stage'
>
export type ManagerHome = { manager: string; employees: ManagerEmployee[] }
export type ManagerCardData = {
  employee: ManagerEmployee
  signals: string[]
  upcoming: { title: string; run_at: string }[]
  notifications: Notification[]
}

/** День испытательного срока: отрицательный — дней до выхода. */
export function probationDay(e: Pick<Employee, 'start_date' | 'probation_end'>) {
  const day = Math.floor((Date.now() - new Date(e.start_date).getTime()) / 86400000) + 1
  const total =
    Math.round((new Date(e.probation_end).getTime() - new Date(e.start_date).getTime()) / 86400000) + 1
  return { day, total }
}

export const PHASES = [
  'Знакомство',
  'До выхода',
  'Первый день',
  'Первые дни',
  'Первый месяц',
  'Второй месяц',
  'Третий месяц и итоги',
]
