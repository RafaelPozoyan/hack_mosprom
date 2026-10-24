import { useQuery } from '@tanstack/react-query'
import clsx from 'clsx'
import { BookOpen, Contact, Inbox, LayoutDashboard, Map, Smile, Users } from 'lucide-react'
import { useState } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router'
import { api, type Ticket } from './api'
import AccessGate from './components/AccessGate'
import { DemoContext, KorsikMark } from './components/ui'
import Dashboard from './pages/Dashboard'
import DigestPage from './pages/Digest'
import EmployeeCard from './pages/EmployeeCard'
import Employees from './pages/Employees'
import Knowledge from './pages/Knowledge'
import ManagerPreview, { ManagerLink } from './pages/Manager'
import Scenario from './pages/Scenario'
import Team from './pages/Team'
import Tickets from './pages/Tickets'

// Роли из кейса: HR-менеджер, руководитель подразделения, руководитель управления по подбору.
// Новый сотрудник общается с Корсиком в MAX — его сторону показывает чат в карточке сотрудника.
const ROLES = {
  hr: { label: 'HR-менеджер', home: '/', nav: ['/', '/employees', '/team', '/tickets', '/kb', '/journey'] },
  manager: { label: 'Руководитель', home: '/my', nav: ['/my'] },
  head: { label: 'Руководитель подбора', home: '/mood', nav: ['/mood', '/', '/employees', '/team'] },
} as const
type Role = keyof typeof ROLES

const NAV: Record<string, { label: string; icon: typeof Users }> = {
  '/': { label: 'Сегодня', icon: LayoutDashboard },
  '/employees': { label: 'Сотрудники', icon: Users },
  '/team': { label: 'Команда', icon: Contact },
  '/my': { label: 'Мои новички', icon: Users },
  '/tickets': { label: 'Обращения', icon: Inbox },
  '/kb': { label: 'База знаний', icon: BookOpen },
  '/mood': { label: 'Настроения', icon: Smile },
  '/journey': { label: 'Путь новичка', icon: Map },
}

const saved = <T,>(key: string, fallback: T): T => {
  const v = localStorage.getItem(key)
  return v === null ? fallback : (JSON.parse(v) as T)
}

export default function App() {
  return (
    <Routes>
      <Route path="/m/:code/*" element={<ManagerLink />} />
      <Route
        path="*"
        element={
          <AccessGate>
            {(logout, protectedPanel) => <Panel logout={logout} protectedPanel={protectedPanel} />}
          </AccessGate>
        }
      />
    </Routes>
  )
}

function Panel({ logout, protectedPanel }: { logout: () => void; protectedPanel: boolean }) {
  const [role, setRole] = useState<Role>(() => saved('korsik_role', 'hr'))
  const [demo, setDemo] = useState<boolean>(() => saved('korsik_demo', true))
  const { data: open = [] } = useQuery({
    queryKey: ['tickets', 'open'],
    queryFn: () => api.get<Ticket[]>('/api/tickets?status=open'),
    refetchInterval: 10_000,
  })
  const persist = (key: string, v: unknown) => localStorage.setItem(key, JSON.stringify(v))

  return (
    <DemoContext.Provider value={{ demo }}>
      <div className="flex min-h-screen">
        <aside className="sticky top-0 flex h-screen w-60 shrink-0 flex-col bg-ink px-4 py-5 text-white">
          <div className="mb-7 flex items-center gap-2.5 px-2">
            <KorsikMark />
            <div className="leading-tight">
              <div className="text-[15px] font-semibold">Корсик</div>
              <div className="text-xs text-white/50">адаптация новичков</div>
            </div>
          </div>
          <nav className="flex flex-col gap-0.5">
            {ROLES[role].nav.map((path) => {
              const { label, icon: Icon } = NAV[path]
              return (
                <NavLink
                  key={path}
                  to={path}
                  end={path === '/'}
                  className={({ isActive }) =>
                    clsx(
                      'flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm transition',
                      isActive
                        ? 'bg-white/[0.12] font-medium text-white'
                        : 'text-white/65 hover:bg-white/[0.06] hover:text-white',
                    )
                  }
                >
                  <Icon size={18} strokeWidth={1.8} />
                  <span className="flex-1">{label}</span>
                  {path === '/tickets' && open.length > 0 && (
                    <span className="rounded-full bg-rose-500 px-1.5 text-[11px] font-semibold">
                      {open.length}
                    </span>
                  )}
                </NavLink>
              )
            })}
          </nav>

          <div className="mt-auto flex flex-col gap-4">
            {protectedPanel && (
              <button
                type="button"
                onClick={logout}
                className="rounded-lg px-3 py-2 text-left text-sm text-white/65 hover:bg-white/[0.06] hover:text-white"
              >
                Выйти из панели
              </button>
            )}
            <div>
              <div className="mb-1.5 px-2 text-[11px] font-medium tracking-wide text-white/40 uppercase">
                Вы вошли как
              </div>
              <div className="flex flex-col gap-0.5">
                {(Object.keys(ROLES) as Role[]).map((r) => (
                  <NavLink
                    key={r}
                    to={ROLES[r].home}
                    onClick={() => {
                      persist('korsik_role', r)
                      setRole(r)
                    }}
                    className={clsx(
                      'rounded-lg px-3 py-1.5 text-[13px] transition',
                      r === role
                        ? 'bg-white/[0.12] font-medium text-white'
                        : 'text-white/55 hover:text-white',
                    )}
                  >
                    {ROLES[r].label}
                  </NavLink>
                ))}
              </div>
            </div>
            <label className="flex cursor-pointer items-start gap-2.5 rounded-xl bg-white/[0.05] p-3">
              <input
                type="checkbox"
                checked={demo}
                onChange={(e) => {
                  persist('korsik_demo', e.target.checked)
                  setDemo(e.target.checked)
                }}
                className="mt-0.5 accent-lime"
              />
              <span className="leading-tight">
                <span className="block text-[13px] font-medium">Режим демонстрации</span>
                <span className="text-[11px] text-white/45">чат глазами новичка и перемотка времени</span>
              </span>
            </label>
          </div>
        </aside>

        <main className="min-w-0 flex-1 px-8 py-7">
          <Routes>
            <Route path="/" element={role === 'manager' ? <Navigate to="/my" /> : <Dashboard />} />
            <Route path="/employees" element={<Employees />} />
            <Route
              path="/employees/:id"
              element={role === 'manager' ? <ManagerPreview card /> : <EmployeeCard />}
            />
            <Route path="/my" element={<ManagerPreview />} />
            <Route path="/my/:id" element={<ManagerPreview card />} />
            <Route path="/team" element={<Team />} />
            <Route path="/tickets" element={<Tickets />} />
            <Route path="/kb" element={<Knowledge />} />
            <Route path="/mood" element={<DigestPage />} />
            <Route path="/journey" element={<Scenario />} />
            <Route path="*" element={<Navigate to={ROLES[role].home} />} />
          </Routes>
        </main>
      </div>
    </DemoContext.Provider>
  )
}
