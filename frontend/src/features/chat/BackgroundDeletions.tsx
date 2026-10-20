import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { api, send } from '../../shared/api/api';
import { Button } from '../../shared/ui/Button';

const storageKey = 'knowledgeops:pending-conversation-deletions';
function readPending(): string[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(storageKey) ?? '[]');
    return Array.isArray(value) ? value.filter((id): id is string => typeof id === 'string') : [];
  } catch {
    return [];
  }
}
function storePending(ids: string[]) {
  try {
    localStorage.setItem(storageKey, JSON.stringify(ids));
  } catch {
    /* 后台作业不依赖浏览器存储。 */
  }
}

const DeletionContext = createContext<{ track: (id: string) => void }>({ track: () => {} });
export const useBackgroundDeletions = () => useContext(DeletionContext);

/** 正常清理静默进行；只保留查询身份以跨导航/刷新报告失败，不在浏览器执行清理。 */
export function BackgroundDeletions({ children }: { children: ReactNode }) {
  const [ids, setIds] = useState(readPending);
  const [failures, setFailures] = useState<Record<string, 'failed' | 'unreachable'>>({});
  const [retrying, setRetrying] = useState<string | null>(null);
  const track = useCallback((id: string) => {
    setIds((previous) => {
      const next = [...new Set([...readPending(), ...previous, id])];
      storePending(next);
      return next;
    });
  }, []);
  const context = useMemo(() => ({ track }), [track]);

  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === storageKey) setIds(readPending());
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);
  useEffect(() => {
    if (!ids.length) return;
    const controller = new AbortController();
    let checking = false;
    async function check() {
      if (checking) return;
      checking = true;
      await Promise.all(
        ids.map(async (id) => {
          try {
            const job = await api<{ status: string }>(`/api/conversations/${id}/deletion`, {
              signal: controller.signal,
            });
            if (controller.signal.aborted) return;
            if (job.status === 'completed') {
              setIds((previous) => {
                const next = previous.filter((item) => item !== id);
                storePending(readPending().filter((item) => item !== id));
                return next;
              });
            }
            setFailures((previous) => {
              const next = { ...previous };
              if (job.status === 'failed') next[id] = 'failed';
              else delete next[id];
              return next;
            });
          } catch {
            if (!controller.signal.aborted)
              setFailures((previous) => ({ ...previous, [id]: 'unreachable' }));
          }
        }),
      );
      checking = false;
    }
    void check();
    const timer = setInterval(() => void check(), 5000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [ids]);

  return (
    <DeletionContext.Provider value={context}>
      {children}
      {Object.entries(failures).filter(([id]) => ids.includes(id)).length > 0 && (
        <div
          className="fixed right-4 bottom-4 z-40 max-h-[40dvh] w-[min(380px,calc(100vw_-_32px))] overflow-auto rounded-xl border border-line bg-white p-4 shadow-lg"
          role="alert"
        >
          {Object.entries(failures)
            .filter(([id]) => ids.includes(id))
            .map(([id, failure]) => (
              <div key={id} className="mb-3 last:mb-0">
                <p className="m-0 text-sm">
                  {failure === 'failed'
                    ? '有一段会话的后台清理未完成，可重试。'
                    : '暂时无法检查后台清理状态，连接恢复后会自动核对。'}
                </p>
                {failure === 'failed' && (
                  <Button
                    className="secondary button-secondary mt-2"
                    disabled={retrying === id}
                    onClick={async () => {
                      setRetrying(id);
                      try {
                        await send(`/api/conversations/${id}/deletion/retry`);
                        setFailures((previous) => {
                          const next = { ...previous };
                          delete next[id];
                          return next;
                        });
                        setIds((previous) => [...previous]);
                      } catch {
                        /* 保留失败提示，可再次重试。 */
                      } finally {
                        setRetrying(null);
                      }
                    }}
                  >
                    重试后台清理
                  </Button>
                )}
              </div>
            ))}
        </div>
      )}
    </DeletionContext.Provider>
  );
}
