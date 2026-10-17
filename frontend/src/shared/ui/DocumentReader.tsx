import { useEffect, useRef, useState } from 'react';
import { Markdown } from './components';

/** 文档预览保留完整正文；源码模式按真实换行定位，不把检索片段当作全文。 */
export function DocumentReader({ text, markdown = false, line }: {
  text: string; markdown?: boolean; line?: number;
}) {
  const [source, setSource] = useState(!markdown || Boolean(line));
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (line && source) root.current?.querySelector(`[data-line="${line}"]`)?.scrollIntoView({ block: 'center' });
  }, [line, source, text]);
  return <div ref={root} className="document-reader">
    {markdown && <div className="reader-toolbar"><button className="secondary small" type="button"
      onClick={() => setSource(!source)}>{source ? '预览文档' : '查看源文本'}</button></div>}
    {source ? <pre className="document-code"><code>{text.split('\n').map((value, index) =>
      <span id={`L${index + 1}`} data-line={index + 1} className={line === index + 1 ? 'focused-line' : ''} key={index}>
        <a href={`#L${index + 1}`} className="line-number" aria-label={`第 ${index + 1} 行`}>{index + 1}</a>
        <span>{value || '\n'}</span>
      </span>)}</code></pre> : <Markdown text={text} />}
  </div>;
}
