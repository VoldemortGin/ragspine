import { useCallback, useEffect, useState } from 'react';
import type { ReactNode } from 'react';

import {
  IconLayers,
  IconMessageSquare,
  IconNetwork,
  IconWorkflow,
  ServerHealthIndicator,
  cn,
} from './components';
import { applyLaunchSession, parseLaunchSessionId } from './launch';
import { JobsPage } from './pages/JobsPage';
import { PlaygroundPage } from './pages/PlaygroundPage';
import { PipelinePage } from './pages/pipeline/PipelinePage';
import { WorkflowsPage } from './pages/workflows';

const PAGE_STORAGE_KEY = 'ragspine-studio.page';

type PageId = 'workflows' | 'playground' | 'pipeline' | 'jobs';

interface NavItem {
  id: PageId;
  label: string;
  icon: ReactNode;
}

const NAV: NavItem[] = [
  { id: 'workflows', label: 'Workflows', icon: <IconWorkflow size={16} /> },
  { id: 'playground', label: 'Playground', icon: <IconMessageSquare size={16} /> },
  { id: 'pipeline', label: 'Pipeline', icon: <IconNetwork size={16} /> },
  { id: 'jobs', label: 'Jobs', icon: <IconLayers size={16} /> },
];

function isPageId(value: unknown): value is PageId {
  return NAV.some((item) => item.id === value);
}

function loadInitialPage(): PageId {
  // A CLI launch session always lands on the workflow editor.
  if (parseLaunchSessionId(window.location.search) !== null) return 'workflows';
  try {
    const stored = localStorage.getItem(PAGE_STORAGE_KEY);
    if (isPageId(stored)) return stored;
  } catch {
    /* storage unavailable */
  }
  return 'workflows';
}

function SpineMark() {
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5 text-indigo-400" aria-hidden="true">
      <g fill="currentColor">
        <rect x="6.5" y="2.5" width="11" height="3.2" rx="1.6" />
        <rect x="8" y="7.4" width="8" height="3.2" rx="1.6" opacity="0.85" />
        <rect x="6.5" y="12.3" width="11" height="3.2" rx="1.6" opacity="0.7" />
        <rect x="9" y="17.2" width="6" height="3.2" rx="1.6" opacity="0.55" />
      </g>
    </svg>
  );
}

const PAGES: Record<PageId, () => ReactNode> = {
  workflows: () => <WorkflowsPage />,
  playground: () => <PlaygroundPage />,
  pipeline: () => <PipelinePage />,
  jobs: () => <JobsPage />,
};

export function App() {
  const [page, setPage] = useState<PageId>(loadInitialPage);
  const [visited, setVisited] = useState<ReadonlySet<PageId>>(() => new Set([loadInitialPage()]));

  useEffect(() => {
    void applyLaunchSession();
  }, []);

  const navigate = useCallback((next: PageId) => {
    setPage(next);
    setVisited((prev) => {
      if (prev.has(next)) return prev;
      const copy = new Set(prev);
      copy.add(next);
      return copy;
    });
    try {
      localStorage.setItem(PAGE_STORAGE_KEY, next);
    } catch {
      /* storage unavailable */
    }
  }, []);

  const active = NAV.find((item) => item.id === page) ?? NAV[0]!;

  return (
    <div className="flex h-screen overflow-hidden bg-zinc-950">
      <aside className="flex w-52 shrink-0 flex-col border-r border-white/10">
        <div className="flex h-12 shrink-0 items-center gap-2 border-b border-white/10 px-4">
          <SpineMark />
          <span className="text-sm font-semibold tracking-tight text-zinc-100">
            RAGSpine <span className="font-normal text-zinc-500">Studio</span>
          </span>
        </div>

        <nav className="flex-1 space-y-0.5 overflow-y-auto px-2 py-3">
          <div className="mb-1.5 px-2 text-[10px] font-semibold tracking-[0.14em] text-zinc-600 uppercase">
            Console
          </div>
          {NAV.map((item) => {
            const isActive = item.id === page;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => navigate(item.id)}
                aria-current={isActive ? 'page' : undefined}
                className={cn(
                  'relative flex w-full items-center gap-2.5 rounded-md px-2.5 py-1.5 text-left text-[13px] transition-colors',
                  isActive
                    ? 'bg-white/[0.06] font-medium text-zinc-100'
                    : 'text-zinc-500 hover:bg-white/[0.03] hover:text-zinc-300',
                )}
              >
                {isActive && (
                  <span className="absolute top-1.5 bottom-1.5 left-0 w-0.5 rounded-full bg-indigo-400" />
                )}
                <span className={isActive ? 'text-indigo-400' : 'text-zinc-600'}>{item.icon}</span>
                {item.label}
              </button>
            );
          })}
        </nav>

        <div className="shrink-0 border-t border-white/10 px-4 py-3">
          <span className="font-mono text-[10px] text-zinc-600">studio 0.1.0</span>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 shrink-0 items-center justify-between border-b border-white/10 px-4">
          <span className="text-[11px] font-semibold tracking-[0.14em] text-zinc-500 uppercase">
            {active.label}
          </span>
          <ServerHealthIndicator />
        </header>

        <main className="min-h-0 flex-1">
          {NAV.map((item) =>
            visited.has(item.id) ? (
              <div key={item.id} className={item.id === page ? 'h-full' : 'hidden'}>
                {PAGES[item.id]()}
              </div>
            ) : null,
          )}
        </main>
      </div>
    </div>
  );
}
