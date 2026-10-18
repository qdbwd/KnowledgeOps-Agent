import { Button } from '../../shared/ui/Button';
import { useState } from 'react';

type Field = {
  type: 'string' | 'integer' | 'number' | 'boolean' | 'array';
  title?: string;
  description?: string;
  default?: unknown;
  enum?: string[];
  oneOf?: { const: string; title?: string }[];
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  items?: { type: string; enum?: string[] };
};
export type MCPInputRequest = {
  id: string;
  mode: 'form' | 'url';
  message: string;
  url?: string;
  requested_schema?: { properties?: Record<string, Field>; required?: string[] };
};
type Reply = { action: 'accept' | 'decline' | 'cancel'; content?: Record<string, unknown> };

/** 展示服务端声明的普通字段；最终类型与范围由后端按保存的原 Schema 校验。 */
export function MCPInputForm({
  requests,
  busy,
  paused,
  submit,
}: {
  requests: MCPInputRequest[];
  busy: boolean;
  paused: boolean;
  submit: (kind: 'mcp' | 'decline', value: Record<string, Reply> | '') => Promise<void>;
}) {
  const [actions, setActions] = useState<Record<string, Reply['action']>>({});
  const [values, setValues] = useState<Record<string, Record<string, unknown>>>(() =>
    Object.fromEntries(
      requests.map((entry) => [
        entry.id,
        Object.fromEntries(
          Object.entries(entry.requested_schema?.properties ?? {})
            .filter(([, field]) => field.default !== undefined)
            .map(([key, field]) => [key, field.default]),
        ),
      ]),
    ),
  );
  function update(id: string, key: string, value: unknown) {
    setValues((current) => ({ ...current, [id]: { ...current[id], [key]: value } }));
  }
  async function send() {
    const replies = Object.fromEntries(
      requests.map((entry) => {
        const action = actions[entry.id] ?? 'accept';
        const reply: Reply = { action };
        if (action === 'accept' && entry.mode === 'form') {
          reply.content = Object.fromEntries(
            Object.entries(values[entry.id] ?? {}).filter(([, value]) => value !== undefined),
          );
        }
        return [entry.id, reply];
      }),
    );
    await submit('mcp', replies);
  }
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        void send();
      }}
    >
      {requests.map((entry) => (
        <fieldset key={entry.id} disabled={busy}>
          <legend>{entry.message}</legend>
          <label>
            处理方式
            <select
              value={actions[entry.id] ?? 'accept'}
              onChange={(event) =>
                setActions((current) => ({
                  ...current,
                  [entry.id]: event.target.value as Reply['action'],
                }))
              }
            >
              <option value="accept">提供信息并继续</option>
              <option value="decline">拒绝提供</option>
              <option value="cancel">取消这项请求</option>
            </select>
          </label>
          {(actions[entry.id] ?? 'accept') === 'accept' &&
            (entry.mode === 'url' ? (
              <div>
                {entry.url && /^https?:\/\//i.test(entry.url) && (
                  <p>
                    <a href={entry.url} target="_blank" rel="noreferrer noopener">
                      打开服务页面完成操作 ↗
                    </a>
                  </p>
                )}
                <p className="muted text-muted small text-xs">
                  请在服务自己的页面操作，不要在聊天中填写密码或认证信息。点击继续后仍由服务核对结果。
                </p>
                <label className="checkbox">
                  <input type="checkbox" required />
                  我已在服务页面操作，继续核对
                </label>
              </div>
            ) : (
              Object.entries(entry.requested_schema?.properties ?? {}).map(([key, field]) => {
                const label = field.title || key;
                const required = entry.requested_schema?.required?.includes(key) ?? false;
                const value = values[entry.id]?.[key];
                const choices =
                  field.enum?.map((item) => ({ const: item, title: item })) ?? field.oneOf;
                return (
                  <label key={key}>
                    {label}
                    {required ? '（必填）' : ''}
                    {field.description && (
                      <span className="muted text-muted small text-xs">{field.description}</span>
                    )}
                    {field.type === 'boolean' ? (
                      <select
                        required={required}
                        value={value === undefined ? '' : String(value)}
                        onChange={(event) =>
                          update(
                            entry.id,
                            key,
                            event.target.value === '' ? undefined : event.target.value === 'true',
                          )
                        }
                      >
                        <option value="">请选择</option>
                        <option value="true">是</option>
                        <option value="false">否</option>
                      </select>
                    ) : field.type === 'array' && field.items?.enum ? (
                      <select
                        multiple
                        required={required}
                        value={Array.isArray(value) ? (value as string[]) : []}
                        onChange={(event) =>
                          update(
                            entry.id,
                            key,
                            Array.from(event.target.selectedOptions, (option) => option.value),
                          )
                        }
                      >
                        {field.items.enum.map((item) => (
                          <option key={item} value={item}>
                            {item}
                          </option>
                        ))}
                      </select>
                    ) : choices ? (
                      <select
                        required={required}
                        value={String(value ?? '')}
                        onChange={(event) => update(entry.id, key, event.target.value || undefined)}
                      >
                        <option value="">请选择</option>
                        {choices.map((item) => (
                          <option key={item.const} value={item.const}>
                            {item.title ?? item.const}
                          </option>
                        ))}
                      </select>
                    ) : field.type === 'array' ? (
                      <textarea
                        required={required}
                        placeholder="每行一项"
                        value={Array.isArray(value) ? value.join('\n') : ''}
                        onChange={(event) => update(entry.id, key, event.target.value.split('\n'))}
                      />
                    ) : (
                      <input
                        type={field.type === 'string' ? 'text' : 'number'}
                        required={required}
                        step={field.type === 'integer' ? 1 : 'any'}
                        min={field.minimum}
                        max={field.maximum}
                        minLength={field.minLength}
                        maxLength={field.maxLength ?? 4000}
                        value={String(value ?? '')}
                        onChange={(event) =>
                          update(
                            entry.id,
                            key,
                            field.type === 'string'
                              ? event.target.value
                              : event.target.value === ''
                                ? undefined
                                : Number(event.target.value),
                          )
                        }
                      />
                    )}
                  </label>
                );
              })
            ))}
        </fieldset>
      ))}
      <p className="muted text-muted small text-xs">
        {paused
          ? '回答会保存，任务保持暂停，仍需明确继续。'
          : '提交后续接原调用；回答不会增加服务或工具权限。'}
      </p>
      <div className="row flex items-center gap-3 wrap flex-wrap">
        <Button type="submit" disabled={busy}>
          提交并继续
        </Button>
        <Button
          type="button"
          className="secondary button-secondary"
          disabled={busy}
          onClick={() => void submit('decline', '')}
        >
          全部暂不提供
        </Button>
      </div>
    </form>
  );
}
