import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { ApiError, AUTH_EXPIRED, checkAccess, clearToken, saveToken } from '../api'
import { Btn, Card, inputCls } from './ui'

export default function AccessGate({
  children,
}: {
  children: (logout: () => void, protectedPanel: boolean) => ReactNode
}) {
  const client = useQueryClient()
  const [status, setStatus] = useState<'checking' | 'locked' | 'open' | 'error'>('checking')
  const [protectedPanel, setProtectedPanel] = useState(false)
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const logout = useCallback(() => {
    clearToken()
    void client.cancelQueries()
    client.clear()
    setPassword('')
    setError('')
    setStatus('locked')
  }, [client])

  useEffect(() => {
    let active = true
    checkAccess().then(
      ({ required }) => {
        if (!active) return
        setProtectedPanel(required)
        setStatus('open')
      },
      (e: unknown) => {
        if (!active) return
        if (e instanceof ApiError && e.status === 401) logout()
        else setStatus('error')
      },
    )
    window.addEventListener(AUTH_EXPIRED, logout)
    return () => {
      active = false
      window.removeEventListener(AUTH_EXPIRED, logout)
    }
  }, [attempt, logout])

  async function login(event: FormEvent) {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const { required } = await checkAccess(password)
      if (required) saveToken(password)
      else clearToken()
      setPassword('')
      setProtectedPanel(required)
      setStatus('open')
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 401
          ? 'Пароль не подошёл. Проверьте его и попробуйте ещё раз.'
          : 'Не удалось связаться с сервером. Проверьте подключение и попробуйте ещё раз.',
      )
    } finally {
      setBusy(false)
    }
  }

  if (status === 'open') return children(logout, protectedPanel)

  return (
    <main className="grid min-h-screen place-items-center px-5 py-10">
      <Card className="w-full max-w-sm">
        <div className="mb-5 flex items-center gap-3">
          <div className="grid size-12 place-items-center rounded-2xl bg-lime text-2xl">🐾</div>
          <div>
            <h1 className="text-xl font-semibold">Корсик</h1>
            <p className="text-sm text-ink/50">Панель адаптации новичков</p>
          </div>
        </div>
        {status === 'checking' && <p role="status">Проверяем доступ…</p>}
        {status === 'error' && (
          <div className="space-y-4">
            <p role="alert" className="text-sm text-ink/70">
              Не удалось связаться с сервером. Проверьте подключение и повторите попытку.
            </p>
            <Btn
              onClick={() => {
                setStatus('checking')
                setAttempt((value) => value + 1)
              }}
            >
              Повторить
            </Btn>
          </div>
        )}
        {status === 'locked' && (
          <form onSubmit={login} className="space-y-4">
            <p className="text-sm text-ink/70">Введите пароль панели, который вам передал администратор.</p>
            <label className="block text-sm font-medium">
              Пароль панели
              <input
                type="password"
                autoComplete="current-password"
                required
                disabled={busy}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                className={`${inputCls} mt-2`}
              />
            </label>
            {error && (
              <p role="alert" className="text-sm text-rose-700">
                {error}
              </p>
            )}
            <Btn type="submit" disabled={busy || !password}>
              {busy ? 'Проверяем…' : 'Войти'}
            </Btn>
            <p className="text-xs text-ink/50">
              Доступ сохраняется в этой вкладке. На общем компьютере выйдите из панели после работы.
            </p>
          </form>
        )}
      </Card>
    </main>
  )
}
