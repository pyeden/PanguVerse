import type { ReactElement, SVGProps } from 'react'

export type IconName =
  | 'spark'
  | 'flow'
  | 'projects'
  | 'atoms'
  | 'tasks'
  | 'play'
  | 'history'
  | 'report'
  | 'plus'
  | 'search'
  | 'close'
  | 'lock'
  | 'unlock'
  | 'warning'
  | 'check'
  | 'chevron'
  | 'arrow'
  | 'settings'
  | 'trash'
  | 'edit'
  | 'refresh'
  | 'bug'
  | 'git'
  | 'download'
  | 'external'
  | 'menu'
  | 'logout'
  | 'code'
  | 'shield'
  | 'clock'
  | 'database'
  | 'copy'
  | 'filter'
  | 'more'
  | 'eye'
  | 'terminal'
  | 'layers'
  | 'bolt'
  | 'user'
  | 'link'
  | 'server'
  | 'file'
  | 'sort'

const paths: Record<IconName, ReactElement> = {
  spark: <><path d="M12 3l1.35 5.05L18 9.5l-4.65 1.45L12 16l-1.35-5.05L6 9.5l4.65-1.45L12 3Z" /><path d="M18.5 15l.65 2.35L21.5 18l-2.35.65L18.5 21l-.65-2.35L15.5 18l2.35-.65L18.5 15Z" /></>,
  flow: <><rect x="3" y="4" width="6" height="5" rx="1.5" /><rect x="15" y="15" width="6" height="5" rx="1.5" /><path d="M9 6.5h2.5a3 3 0 0 1 3 3v4a3 3 0 0 0 3 3H18" /></>,
  projects: <><path d="M3.5 7.5h6l1.7 2H20v8.7a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2V7.5Z" /><path d="M3.5 9.5V6a2 2 0 0 1 2-2h3l1.7 2h6.3a2 2 0 0 1 2 2v1.5" /></>,
  atoms: <><circle cx="12" cy="12" r="2.2" /><path d="M4.2 9.1c1.7-3 5.3-4.7 8.3-4.1M19.8 14.9c-1.7 3-5.3 4.7-8.3 4.1M4.7 16c-1.9-2.3-2.4-5.3-1.4-7.4M19.3 8c1.9 2.3 2.4 5.3 1.4 7.4" /><path d="M8.3 4.9c2.8-1 5.6.2 7.3 2.5M15.7 19.1c-2.8 1-5.6-.2-7.3-2.5M3 11c.2 2.1 2.2 4.1 5 5.1M21 13c-.2-2.1-2.2-4.1-5-5.1" /></>,
  tasks: <><rect x="4" y="3.5" width="16" height="17" rx="2.5" /><path d="M8 8h8M8 12h5M8 16h7" /><path d="M6.5 8h.01M6.5 12h.01M6.5 16h.01" /></>,
  play: <path d="M8 5.2v13.6a.8.8 0 0 0 1.2.7l10.4-6.8a.8.8 0 0 0 0-1.4L9.2 4.5A.8.8 0 0 0 8 5.2Z" />,
  history: <><path d="M3.5 12a8.5 8.5 0 1 0 2.5-6" /><path d="M3.5 4.5V10H9" /><path d="M12 7.5V12l3 2" /></>,
  report: <><path d="M6 3.5h8l4 4v13H6v-17Z" /><path d="M14 3.5v4h4M9 12h6M9 15.5h6M9 8.5h2" /></>,
  plus: <path d="M12 5v14M5 12h14" />,
  search: <><circle cx="10.5" cy="10.5" r="6" /><path d="m15 15 4.5 4.5" /></>,
  close: <path d="M6 6l12 12M18 6 6 18" />,
  lock: <><rect x="5" y="10" width="14" height="10" rx="2" /><path d="M8 10V7.5a4 4 0 0 1 8 0V10M12 14v2" /></>,
  unlock: <><rect x="5" y="10" width="14" height="10" rx="2" /><path d="M8 10V7.5a4 4 0 0 1 7.5-2M12 14v2" /></>,
  warning: <><path d="M12 4 21 20H3L12 4Z" /><path d="M12 9v5M12 17.5v.1" /></>,
  check: <path d="m5 12.5 4.3 4.3L19 7" />,
  chevron: <path d="m9 6 6 6-6 6" />,
  arrow: <path d="M5 12h14M14 7l5 5-5 5" />,
  settings: <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.86 2.86-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.03 1.56V21h-4.04v-.08A1.7 1.7 0 0 0 8.9 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.86-2.86.06-.06A1.7 1.7 0 0 0 4.5 15a1.7 1.7 0 0 0-1.56-1.03H3v-4.04h.08A1.7 1.7 0 0 0 4.6 8.9a1.7 1.7 0 0 0-.34-1.88l-.06-.06L7.06 4.1l.06.06A1.7 1.7 0 0 0 9 4.5h.02A1.7 1.7 0 0 0 10.04 3H14v.08A1.7 1.7 0 0 0 15.1 4.6a1.7 1.7 0 0 0 1.88-.34l.06-.06 2.86 2.86-.06.06A1.7 1.7 0 0 0 19.5 9v.02A1.7 1.7 0 0 0 21 10.04V14h-.08A1.7 1.7 0 0 0 19.4 15Z" /></>,
  trash: <><path d="M4 7h16M9 7V4h6v3M6.5 7l1 13h9l1-13M10 11v5M14 11v5" /></>,
  edit: <><path d="M4 20h4l10.8-10.8a2 2 0 0 0-2.8-2.8L5 17.2 4 20Z" /><path d="m13.5 7.5 3 3" /></>,
  refresh: <><path d="M20 7v5h-5" /><path d="M19.2 12a7.5 7.5 0 1 0-2.1 5.3" /></>,
  bug: <><rect x="7" y="8" width="10" height="12" rx="5" /><path d="M9 5.5 11 8M15 5.5 13 8M3.5 11H7M17 11h3.5M3.5 16H7M17 16h3.5M12 8v12" /></>,
  git: <><circle cx="6" cy="5" r="2" /><circle cx="18" cy="19" r="2" /><circle cx="6" cy="19" r="2" /><path d="M6 7v10M8 5h5a5 5 0 0 1 5 5v7" /></>,
  download: <><path d="M12 3v12M7 10l5 5 5-5" /><path d="M4 21h16" /></>,
  external: <><path d="M14 4h6v6M20 4l-8 8" /><path d="M18 14v5H5V6h5" /></>,
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  logout: <><path d="M10 4H5v16h5M14 8l4 4-4 4M9 12h9" /></>,
  code: <><path d="m8 7-5 5 5 5M16 7l5 5-5 5M14 4l-4 16" /></>,
  shield: <><path d="M12 3 5 6v5c0 4.5 3 8 7 10 4-2 7-5.5 7-10V6l-7-3Z" /><path d="m9 12 2 2 4-4" /></>,
  clock: <><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></>,
  database: <><ellipse cx="12" cy="5.5" rx="7" ry="3" /><path d="M5 5.5v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6M5 11.5v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6" /></>,
  copy: <><rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3" /></>,
  filter: <path d="M3 5h18l-7 8v5l-4 2v-7L3 5Z" />,
  more: <><circle cx="5" cy="12" r="1.2" fill="currentColor" stroke="none" /><circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none" /><circle cx="19" cy="12" r="1.2" fill="currentColor" stroke="none" /></>,
  eye: <><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z" /><circle cx="12" cy="12" r="3" /></>,
  terminal: <><rect x="3" y="4" width="18" height="16" rx="2.5" /><path d="m7 9 3 3-3 3M12.5 15H17" /></>,
  layers: <><path d="m12 3 9 5-9 5-9-5 9-5Z" /><path d="m3 12 9 5 9-5M3 16l9 5 9-5" /></>,
  bolt: <path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z" />,
  user: <><circle cx="12" cy="8" r="4" /><path d="M4.5 21a7.5 7.5 0 0 1 15 0" /></>,
  link: <><path d="M10 13a4.5 4.5 0 0 0 6.4.2l2.2-2.2a4.5 4.5 0 0 0-6.4-6.4L11 6" /><path d="M14 11a4.5 4.5 0 0 0-6.4-.2l-2.2 2.2a4.5 4.5 0 0 0 6.4 6.4L13 18" /></>,
  server: <><rect x="3" y="4" width="18" height="6" rx="2" /><rect x="3" y="14" width="18" height="6" rx="2" /><path d="M7 7h.01M7 17h.01M11 7h6M11 17h6" /></>,
  file: <><path d="M6 3.5h8l4 4v13H6v-17Z" /><path d="M14 3.5v4h4" /></>,
  sort: <path d="M8 5v14M5 8l3-3 3 3M16 19V5M13 16l3 3 3-3" />,
}

export function Icon({ name, size = 18, ...props }: { name: IconName; size?: number } & SVGProps<SVGSVGElement>) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      {paths[name]}
    </svg>
  )
}
