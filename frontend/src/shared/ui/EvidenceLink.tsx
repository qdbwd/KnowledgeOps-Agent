import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useResource } from '../api/api';
import type { Evidence } from '../types/types';

export function evidenceTarget(evidence: Evidence): string | null {
  if (evidence.origin && /^https?:\/\//i.test(evidence.origin)) return evidence.origin;
  if (evidence.document_id) return `/knowledge/${evidence.document_id}?version=${encodeURIComponent(evidence.knowledge_version_id ?? '')}`;
  if (evidence.source_id && evidence.source_version_id) return `/sources/${evidence.source_id}?version=${encodeURIComponent(evidence.source_version_id)}${evidence.page ? `&page=${evidence.page}` : ''}`;
  return null;
}

/** 读取已存在的证据身份解析目标；不再次搜索，不登记新的模型阅读。 */
export function EvidenceLink({ id, children }: { id: string; children: ReactNode }) {
  const { data } = useResource<Evidence>(`/api/evidence/${id}`);
  const href = data ? evidenceTarget(data) : null;
  return href && /^https?:\/\//i.test(href)
    ? <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>
    : <Link to={href ?? `/evidence/${id}`}>{children}</Link>;
}
