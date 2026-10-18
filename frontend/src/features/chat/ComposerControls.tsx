import { Button } from '../../shared/ui/Button';
import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { ArrowUp, Plus, ShieldCheck, X } from 'lucide-react';
import type { Source } from '../../shared/types/types';

type Props = {
  conversationId?: string;
  permission: string;
  reasoning: string;
  model?: string;
  modelEnabled?: boolean;
  settingBusy: boolean;
  changeSetting: (field: 'permission_mode' | 'reasoning_effort', value: string) => void;
  capabilities?: {
    levels: string[];
    default_level: string | null;
    can_disable: boolean;
    note: string;
  } | null;
  mode: 'ask' | 'preview' | 'direct';
  setMode: (value: 'ask' | 'preview' | 'direct') => void;
  knowledgeOnly: boolean;
  setKnowledgeOnly: (value: boolean) => void;
  delegation: boolean;
  setDelegation: (value: boolean) => void;
  auxiliary: boolean;
  setAuxiliary: (value: boolean) => void;
  materials: Source[];
  selected: string[];
  setSelected: (value: string[]) => void;
  disabled: boolean;
  busy: boolean;
  context: ReactNode;
};

const reasoningLabels: Record<string, string> = {
  low: '低',
  medium: '中',
  high: '高',
  max: '最高',
  none: '无',
};

/** 常用控制常驻底栏，其余选项渐进展开；收起菜单不会重置用户选择。 */
export function ComposerControls(props: Props) {
  const [open, setOpen] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!container.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setOpen(false);
        trigger.current?.focus();
      }
    };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', escape);
    };
  }, [open]);
  return (
    <>
      {props.selected.length > 0 && (
        <div
          className="composer-attachments flex max-h-[76px] flex-wrap gap-1.5 overflow-auto"
          aria-label="已关联材料"
        >
          {props.selected.map((id) => (
            <Button
              key={id}
              type="button"
              className="attachment-chip max-w-[230px] overflow-hidden px-2 py-[3px] text-[11px] text-ellipsis secondary button-secondary"
              onClick={() => props.setSelected(props.selected.filter((value) => value !== id))}
              title="取消关联此材料"
            >
              {props.materials.find((item) => item.id === id)?.title ?? '已选材料'} <X size={12} />
            </Button>
          ))}
        </div>
      )}
      <div className="composer-toolbar mt-2.5 flex items-center justify-between gap-2 max-md:gap-[3px]">
        <div className="composer-toolbar-left flex shrink-0 items-center gap-2 max-md:gap-[3px]">
          <div ref={container} className="composer-menu relative">
            <Button
              ref={trigger}
              type="button"
              className="quiet button-quiet icon-button size-[34px] shrink-0 rounded-full p-2 max-md:h-8 max-md:w-[30px]"
              aria-label="更多设置与材料"
              aria-expanded={open}
              aria-controls="composer-extra-options"
              onClick={() => setOpen(!open)}
            >
              <Plus size={21} />
            </Button>
            {open && (
              <div
                id="composer-extra-options"
                className="composer-popover absolute bottom-[calc(100%_+_16px)] left-0 z-20 max-h-[min(500px,55dvh)] w-[min(310px,calc(100vw_-_64px))] overflow-y-auto rounded-xl border border-line bg-white p-4 text-[13px] shadow-[0_10px_35px_#183d3525] [&>*]:mb-3 [&_label]:my-2.5 [&_label]:gap-2 [&_.check]:justify-start"
                role="region"
                aria-label="更多设置与材料"
              >
                <strong>本次任务</strong>
                <label>
                  知识变更
                  <select
                    value={props.mode}
                    onChange={(e) => props.setMode(e.target.value as Props['mode'])}
                  >
                    <option value="ask">需要时询问</option>
                    <option value="preview">先看草稿</option>
                    <option value="direct">本次直接保存</option>
                  </select>
                </label>
                <label className="check m-0 flex-row items-center gap-2 font-normal">
                  <input
                    type="checkbox"
                    checked={props.knowledgeOnly}
                    onChange={(e) => props.setKnowledgeOnly(e.target.checked)}
                  />
                  仅使用知识库
                </label>
                <label className="check m-0 flex-row items-center gap-2 font-normal">
                  <input
                    type="checkbox"
                    checked={props.delegation}
                    onChange={(e) => props.setDelegation(e.target.checked)}
                  />
                  允许子Agent分工
                </label>
                <label
                  className="check m-0 flex-row items-center gap-2 font-normal"
                  title="按需增加模型请求，共享本任务预算"
                >
                  <input
                    type="checkbox"
                    checked={props.auxiliary}
                    onChange={(e) => props.setAuxiliary(e.target.checked)}
                  />
                  允许辅助模型
                </label>
                {props.conversationId && (
                  <Link to={`/tools?conversation_id=${props.conversationId}`}>命令规则</Link>
                )}
                <div className="composer-materials border-t border-line pt-3 [&_label]:items-start">
                  <strong>关联材料</strong>
                  {props.materials.map((source) => (
                    <label
                      className="check m-0 flex-row items-center gap-2 font-normal"
                      key={source.id}
                    >
                      <input
                        type="checkbox"
                        checked={props.selected.includes(source.id)}
                        onChange={(e) =>
                          props.setSelected(
                            e.target.checked
                              ? [...props.selected, source.id]
                              : props.selected.filter((id) => id !== source.id),
                          )
                        }
                      />
                      {source.title}
                    </label>
                  ))}
                  <Link to="/sources">导入或管理材料 →</Link>
                </div>
                <small className="muted text-muted">Ctrl / ⌘ + Enter 发送，Enter 换行</small>
              </div>
            )}
          </div>
          <label
            className="composer-permission m-0 flex-row items-center gap-[3px] max-md:[&>svg]:hidden"
            title="本会话后续任务的执行权限"
          >
            <ShieldCheck size={15} />
            <span className="sr-only">执行权限</span>
            <select
              className="h-[34px] max-w-[138px] border-0 bg-transparent px-0.5 py-0 text-xs text-control max-md:h-8 max-md:max-w-[92px] max-md:text-[11px]"
              aria-label="执行权限"
              value={props.permission}
              disabled={props.settingBusy}
              onChange={(e) => props.changeSetting('permission_mode', e.target.value)}
            >
              <option value="ask">请求批准</option>
              <option value="auto">自动执行</option>
            </select>
          </label>
        </div>
        <div className="composer-toolbar-right flex min-w-0 items-center justify-end gap-2 max-md:gap-[3px]">
          {props.context}
          <span
            className="composer-model min-w-0 max-w-[180px] truncate text-xs leading-[34px] text-control max-md:max-w-[100px] max-md:text-[11px] max-md:leading-8"
            title={`${props.model ?? '正在读取模型'} · ${props.modelEnabled === undefined ? '正在读取状态' : props.modelEnabled ? '模型调用已开启' : '模型调用关闭'}`}
          >
            {props.model ?? '读取模型…'}
          </span>
          {props.capabilities?.levels?.length ? (
            <label
              className="composer-reasoning m-0 shrink-0 flex-row items-center gap-[3px]"
              title={props.capabilities.note}
            >
              <span className="sr-only">思考强度</span>
              <select
                className="h-[34px] max-w-[138px] border-0 bg-transparent px-0.5 py-0 text-xs text-control max-md:h-8 max-md:max-w-[92px] max-md:text-[11px]"
                aria-label="思考强度"
                value={props.reasoning}
                disabled={props.settingBusy}
                onChange={(e) => props.changeSetting('reasoning_effort', e.target.value)}
              >
                <option value="default">
                  默认
                  {props.capabilities.default_level
                    ? `（${reasoningLabels[props.capabilities.default_level] ?? props.capabilities.default_level}）`
                    : ''}
                </option>
                {props.capabilities.can_disable && <option value="off">关闭</option>}
                {props.capabilities.levels.map((level) => (
                  <option key={level} value={level}>
                    {reasoningLabels[level] ?? level}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <span className="muted text-muted small text-xs" title="思考强度由供应商决定">
              默认思考
            </span>
          )}
          <Button
            className="send icon-button size-[34px] shrink-0 rounded-full p-2 max-md:h-8 max-md:w-[30px]"
            disabled={props.disabled}
            type="submit"
            aria-label="发送"
            title={props.busy ? '提交中…' : '发送（Ctrl / ⌘ + Enter）'}
          >
            <ArrowUp size={20} />
          </Button>
        </div>
      </div>
    </>
  );
}
