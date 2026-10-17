import { useCallback, useMemo, useState } from 'react';
import { BrowserRouter, Link, NavLink, Route, Routes } from 'react-router-dom';
import { BookOpen, Files, GitPullRequest, MessageSquare, Plus, Sprout } from 'lucide-react';
import { useResource } from '../shared/api/api';
import { ChatPage } from '../features/chat/ChatPage';
import { DeleteConversationPage } from '../features/chat/DeleteConversationPage';
import { ArticlePage, KnowledgePage } from '../features/knowledge/KnowledgePage';
import { ChangesPage, ChangePage } from '../features/changes/ChangesPage';
import { SourcesPage, SourcePage, EvidencePage } from '../features/sources/SourcesPage';
import { WorkspaceFilePage } from '../features/sources/WorkspaceFilePage';
import { RunsPage, RunPage, RunArtifactPage } from '../features/runs/RunsPage';
import { PreferencesPage } from '../features/memory/PreferencesPage';
import { LearningPage } from '../features/learning/LearningPage';
import { LearningProfilesPage } from '../features/learning/LearningProfilesPage';
import { MCPPage } from '../features/mcp/MCPPage';
import { SubagentsPage } from '../features/subagents/SubagentsPage';
import { ToolsPage } from '../features/tools/ToolsPage';
import { ErrorBox } from '../shared/ui/components';
import { WorkspaceContext } from '../shared/state/workspace';
import type { Conversation } from '../shared/types/types';

export default function App() {
  const [version, setVersion] = useState(0);
  const refresh = useCallback(() => setVersion((value) => value + 1), []);
  const workspace = useMemo(() => ({ version, refresh }), [version, refresh]);
  const conversations = useResource<Conversation[]>('/api/conversations', version);
  const settings = useResource<{ model: string; model_calls_enabled: boolean }>(
    '/api/settings',
    version,
  );
  return (
    <BrowserRouter>
      <WorkspaceContext.Provider value={workspace}>
        <div className="app-shell">
          <aside className="sidebar">
            <Link className="brand" to="/">
              <span>
                <Sprout size={23} />
              </span>
              <div>
                KnowledgeOps<small>个人知识工作台</small>
              </div>
            </Link>
            <Link className="new-chat" to="/">
              <Plus size={17} />
              开始新对话
            </Link>
            <nav aria-label="主要功能">
              <NavLink to="/" end>
                <MessageSquare size={18} />
                对话
              </NavLink>
              <NavLink to="/knowledge">
                <BookOpen size={18} />
                知识库
              </NavLink>
              <NavLink to="/learning">
                <BookOpen size={18} />
                学习与整理
              </NavLink>
              <NavLink to="/sources">
                <Files size={18} />
                材料与来源
              </NavLink>
              <NavLink to="/changes">
                <GitPullRequest size={18} />
                变更与运行
              </NavLink>
              <NavLink to="/memories">
                <BookOpen size={18} />
                长期记忆
              </NavLink>
              <NavLink to="/tools">
                <Files size={18} />
                工具与能力
              </NavLink>
              <NavLink to="/subagents">
                <Files size={18} />
                子Agent
              </NavLink>
              <NavLink to="/mcp">
                <Files size={18} />
                工具服务
              </NavLink>
            </nav>
            <div className="recent">
              <p>最近对话</p>
              {conversations.data?.slice(0, 12).map((chat) => (
                <NavLink to={`/chat/${chat.id}`} key={chat.id}>
                  {chat.title}
                </NavLink>
              ))}
              {conversations.data?.length === 0 && (
                <span className="muted small">从右侧开始第一段对话</span>
              )}
            </div>
            <details className="mobile-conversations">
              <summary>最近对话</summary>
              {conversations.data?.map((chat) => (
                <NavLink to={`/chat/${chat.id}`} key={chat.id}>
                  {chat.title}
                </NavLink>
              ))}
              {conversations.data?.length === 0 && <p>还没有对话。</p>}
            </details>
          </aside>
          <main className="main">
            <div className="main-content">
              <ErrorBox message={settings.error || conversations.error} />
              <Routes>
                <Route path="/" element={<ChatPage model={settings.data?.model} modelEnabled={settings.data?.model_calls_enabled} />} />
                <Route path="/chat/:conversationId" element={<ChatPage model={settings.data?.model} modelEnabled={settings.data?.model_calls_enabled} />} />
                <Route path="/chat/:conversationId/deletion" element={<DeleteConversationPage />} />
                <Route path="/knowledge" element={<KnowledgePage />} />
                <Route path="/knowledge/:documentId" element={<ArticlePage />} />
                <Route path="/sources" element={<SourcesPage />} />
                <Route path="/files" element={<WorkspaceFilePage />} />
                <Route path="/sources/:sourceId" element={<SourcePage />} />
                <Route path="/evidence/:evidenceId" element={<EvidencePage />} />
                <Route path="/changes" element={<ChangesPage />} />
                <Route path="/changes/:changesetId" element={<ChangePage />} />
                <Route path="/runs" element={<RunsPage />} />
                <Route path="/runs/:runId" element={<RunPage />} />
                <Route path="/runs/:runId/artifacts/:artifactId" element={<RunArtifactPage />} />
                <Route path="/preferences" element={<PreferencesPage />} />
                <Route path="/memories" element={<PreferencesPage />} />
                <Route path="/learning" element={<LearningPage />} />
                <Route path="/learning/:taskId" element={<LearningPage />} />
                <Route path="/learning-profiles" element={<LearningProfilesPage />} />
                <Route path="/mcp" element={<MCPPage />} />
                <Route path="/subagents" element={<SubagentsPage />} />
                <Route path="/tools" element={<ToolsPage />} />
                <Route
                  path="*"
                  element={
                    <p>
                      页面不存在。<Link to="/">返回工作台</Link>
                    </p>
                  }
                />
              </Routes>
            </div>
          </main>
        </div>
      </WorkspaceContext.Provider>
    </BrowserRouter>
  );
}
