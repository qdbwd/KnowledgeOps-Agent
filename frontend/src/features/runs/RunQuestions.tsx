import { useState } from 'react';
import { messageOf, send, useResource } from '../../shared/api/api';
import type { Run } from '../../shared/types/types';
import { ErrorBox } from '../../shared/ui/components';
import { MCPInputForm, type MCPInputRequest } from './MCPInputForm';

type Question = {
  id: string;
  revision: number;
  status: string;
  kind?: string;
  request: { question: string; options?: string[]; context?: string; requests?: MCPInputRequest[] };
  answer: { kind: string; value: unknown } | null;
};

function QuestionCard({ question, run, changed }: { question: Question; run: Run; changed: () => void }) {
  const [value, setValue] = useState('');
  const [kind, setKind] = useState('text');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const pending = question.status === 'pending';
  const canAnswer = pending && !run.cancel_requested && ['running', 'queued', 'waiting_children', 'waiting_input', 'paused', 'interrupted'].includes(run.status);
  async function answer(responseKind: string, responseValue: unknown = value) {
    setBusy(true);
    setError('');
    try {
      await send(`/api/runs/${run.root_id}/questions/${question.id}/answer`, {
        expected_revision: question.revision, kind: responseKind,
        value: responseKind === 'decline' ? '' : responseValue,
      });
      changed();
    } catch (failure) {
      setError(messageOf(failure));
      changed();
    } finally {
      setBusy(false);
    }
  }
  return (
    <article className="list-card">
      <div style={{ width: '100%' }}>
        <strong>{question.request.question}</strong>
        {question.request.context && <p className="muted">{question.request.context}</p>}
        <ErrorBox message={error} />
        {canAnswer && question.kind === 'mcp'
          ? <MCPInputForm requests={question.request.requests ?? []} busy={busy} paused={run.status === 'paused'} submit={answer} />
          : canAnswer ? <form onSubmit={(event) => { event.preventDefault(); void answer(kind); }}>
          {!!question.request.options?.length && <div className="row wrap">
            {question.request.options.map((option) => <button key={option} type="button" className="secondary small"
              aria-pressed={kind === 'option' && value === option} disabled={busy}
              onClick={() => { setValue(option); setKind('option'); }}>{option}</button>)}
          </div>}
          <label>你的回答
            <textarea value={value} maxLength={12000} disabled={busy} rows={3}
              onChange={(event) => { setValue(event.target.value); setKind('text'); }} />
          </label>
          <p className="muted small">{run.status === 'paused'
            ? '回答会保存，任务保持暂停，仍需明确继续。'
            : '回答后继续原任务；这是补充信息，不是批准执行操作。'}</p>
          <div className="row wrap">
            <button type="submit" disabled={busy || !value.trim()}>提交回答</button>
            <button type="button" className="secondary" disabled={busy} onClick={() => void answer('decline')}>暂不回答</button>
          </div>
        </form> : <p>{question.status === 'answered' ? (question.kind === 'mcp'
          ? '补充信息已保存；是否完成由服务返回结果确认。' : `你的回答：${question.answer?.value}`)
          : question.status === 'declined' ? '你选择了暂不回答。'
          : question.status === 'stale' ? '要求已更新，原问题已失效。'
          : '任务已停止，原问题保留供查阅。'}</p>}
      </div>
    </article>
  );
}

/** 问题与答案来自服务端，重新打开会话可恢复；不会将任意新消息当作答案。 */
export function RunQuestions({ run, refreshKey, onChanged }: {
  run: Run; refreshKey: string | number; onChanged: () => void;
}) {
  const [version, setVersion] = useState(0);
  const questions = useResource<Question[]>(run.question_count ? `/api/runs/${run.root_id}/questions` : null, `${refreshKey}:${version}`);
  function changed() { setVersion((n) => n + 1); onChanged(); }
  if (!questions.data?.length) return <ErrorBox message={questions.error} />;
  return <section className="card">
    <h2>需要你的补充</h2>
    <ErrorBox message={questions.error} />
    {questions.data.map((question) => <QuestionCard key={question.id} question={question} run={run} changed={changed} />)}
  </section>;
}
