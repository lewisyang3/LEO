import { useEffect, useState } from 'react'

export type Route =
  | { page: 'findings' }
  | { page: 'finding'; id: string }
  | { page: 'actions' }

export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent)
  if (parts[0] === 'findings' && parts[1]) return { page: 'finding', id: parts[1] }
  if (parts[0] === 'actions') return { page: 'actions' }
  return { page: 'findings' }
}

export function useHashRoute(): Route {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash))
  useEffect(() => {
    const onChange = () => {
      setRoute(parseRoute(window.location.hash))
      window.scrollTo(0, 0)
    }
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  return route
}

export const findingHref = (id: string) => `#/findings/${encodeURIComponent(id)}`
