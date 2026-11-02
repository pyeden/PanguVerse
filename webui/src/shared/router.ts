import { useEffect, useState } from 'react'

export interface Route {
  path: string
  parts: string[]
  query: URLSearchParams
}

export function parseHash(hash = window.location.hash): Route {
  const raw = (hash || '').replace(/^#/, '') || '/projects'
  const qIndex = raw.indexOf('?')
  const path = qIndex >= 0 ? raw.slice(0, qIndex) : raw
  const query = new URLSearchParams(qIndex >= 0 ? raw.slice(qIndex + 1) : '')
  const parts = path.split('/').filter(Boolean).map((part) => {
    try {
      return decodeURIComponent(part)
    } catch {
      return part
    }
  })
  return { path, parts, query }
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseHash())
  useEffect(() => {
    const onChange = () => setRoute(parseHash())
    window.addEventListener('hashchange', onChange)
    if (!window.location.hash) {
      history.replaceState(null, '', '#/projects')
      setRoute(parseHash('#/projects'))
    }
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  return route
}

export function navigate(to: string, options: { replace?: boolean } = {}): void {
  const target = to.startsWith('#') ? to : `#${to.startsWith('/') ? '' : '/'}${to}`
  if (window.location.hash === target) {
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    return
  }
  if (options.replace) {
    history.replaceState(null, '', target)
    window.dispatchEvent(new HashChangeEvent('hashchange'))
  } else {
    window.location.hash = target
  }
}

export function href(path: string): string {
  return `#${path.startsWith('/') ? path : `/${path}`}`
}

export function withQuery(path: string, query: URLSearchParams | Record<string, string | number | undefined | null>): string {
  const params = query instanceof URLSearchParams ? new URLSearchParams(query) : new URLSearchParams()
  if (!(query instanceof URLSearchParams)) {
    Object.entries(query).forEach(([key, value]) => {
      if (value !== undefined && value !== null && String(value) !== '') params.set(key, String(value))
    })
  }
  const text = params.toString()
  return text ? `${path}?${text}` : path
}
