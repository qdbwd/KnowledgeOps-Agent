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

const navigationStyle =
  'flex items-center gap-[13px] rounded-[7px] px-3.5 py-2.5 text-sm text-[#aebeb4] hover:bg-[#244237] hover:no-underline [&.active]:bg-[#28483b] [&.active]:text-[#edf5e8] max-md:shrink-0 max-md:gap-1.5 max-md:px-2.5 max-md:py-2 max-md:text-xs';

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
        <div className="app-shell flex min-h-screen max-md:block has-[.chat-page]:h-dvh has-[.chat-page]:overflow-hidden max-md:has-[.chat-page]:flex max-md:has-[.chat-page]:flex-col">
          <aside className="sidebar fixed inset-y-0 left-0 flex w-[244px] shrink-0 flex-col overflow-y-auto bg-sidebar px-4.5 pt-7 pb-5 text-[#d5e1d9] max-lg:w-[214px] max-md:static max-md:w-auto max-md:p-[15px]">
            <Link
              className="brand mx-1.5 mb-8 flex items-center gap-2.5 text-[19px] font-[650] tracking-[-0.4px] text-[#f0f5ed] hover:no-underline max-md:mx-0 max-md:mb-3.5 max-md:text-lg"
              to="/"
            >
              <span className="grid h-[39px] w-[35px] place-items-center rounded-[11px] border border-[#557164] text-[#c3dcb2]">
                <Sprout size={23} />
              </span>
              <div>
                KnowledgeOps
                <small className="block text-[10px] font-normal tracking-[2.8px] text-[#a9bdb0] max-md:hidden">
                  个人知识工作台
                </small>
              </div>
            </Link>
            <Link
              className="new-chat mb-[23px] flex items-center justify-center gap-2 rounded-lg border border-[#526d60] p-2.5 text-sm text-[#e3ece1] max-md:mb-3 max-md:inline-flex"
              to="/"
            >
              <Plus size={17} />
              开始新对话
            </Link>
            <nav
              aria-label="主要功能"
              className="grid gap-1.5 max-md:flex max-md:gap-1 max-md:overflow-x-auto max-md:pb-1.5 max-md:[&_svg]:w-3.5"
            >
              <NavLink className={navigationStyle} to="/" end>
                <MessageSquare size={18} />
                对话
              </NavLink>
              <NavLink className={navigationStyle} to="/knowledge">
                <BookOpen size={18} />
                知识库
              </NavLink>
              <NavLink className={navigationStyle} to="/learning">
                <BookOpen size={18} />
                学习与整理
              </NavLink>
              <NavLink className={navigationStyle} to="/sources">
                <Files size={18} />
                材料与来源
              </NavLink>
              <NavLink className={navigationStyle} to="/changes">
                <GitPullRequest size={18} />
                变更与运行
              </NavLink>
              <NavLink className={navigationStyle} to="/memories">
                <BookOpen size={18} />
                长期记忆
              </NavLink>
              <NavLink className={navigationStyle} to="/tools">
                <Files size={18} />
                工具与能力
              </NavLink>
              <NavLink className={navigationStyle} to="/subagents">
                <Files size={18} />
                子Agent
              </NavLink>
              <NavLink className={navigationStyle} to="/mcp">
                <Files size={18} />
                工具服务
              </NavLink>
            </nav>
            <div className="recent mt-[35px] min-h-0 overflow-y-auto px-[7px] max-md:hidden">
              <p className="mx-[7px] mt-0 mb-2.5 text-[11px] tracking-[1px] text-[#86a093]">
                最近对话
              </p>
              {conversations.data?.slice(0, 12).map((chat) => (
                <NavLink
                  to={`/chat/${chat.id}`}
                  key={chat.id}
                  className="block truncate rounded-[5px] px-[7px] py-[9px] text-xs text-[#b2c4b7] hover:bg-[#244237] hover:no-underline [&.active]:bg-[#27483b] [&.active]:text-[#f0f5e8]"
                >
                  {chat.title}
                </NavLink>
              ))}
              {conversations.data?.length === 0 && (
                <span className="muted text-muted small text-xs">从右侧开始第一段对话</span>
              )}
            </div>
            <details className="mobile-conversations mt-2.5 hidden text-xs max-md:block open:max-h-[220px] open:overflow-auto">
              <summary>最近对话</summary>
              {conversations.data?.map((chat) => (
                <NavLink
                  to={`/chat/${chat.id}`}
                  key={chat.id}
                  className="block px-1 py-2 text-[#d5e1d9] [overflow-wrap:anywhere]"
                >
                  {chat.title}
                </NavLink>
              ))}
              {conversations.data?.length === 0 && <p>还没有对话。</p>}
            </details>
          </aside>
          <main className="main ml-[244px] min-w-0 flex-1 px-11 pt-[35px] pb-[42px] max-lg:ml-[214px] max-lg:p-7 max-md:ml-0 max-md:px-4 max-md:py-6 has-[.chat-page]:flex has-[.chat-page]:min-h-0 has-[.chat-page]:flex-col has-[.chat-page]:pb-4 max-md:has-[.chat-page]:px-2.5 max-md:has-[.chat-page]:pt-3 max-md:has-[.chat-page]:pb-[max(10px,env(safe-area-inset-bottom))]">
            <div className="main-content mx-auto w-full max-w-[1512px] has-[.chat-page]:flex has-[.chat-page]:min-h-0 has-[.chat-page]:flex-1 has-[.chat-page]:flex-col">
              <ErrorBox message={settings.error || conversations.error} />
              <Routes>
                <Route
                  path="/"
                  element={
                    <ChatPage
                      model={settings.data?.model}
                      modelEnabled={settings.data?.model_calls_enabled}
                    />
                  }
                />
                <Route
                  path="/chat/:conversationId"
                  element={
                    <ChatPage
                      model={settings.data?.model}
                      modelEnabled={settings.data?.model_calls_enabled}
                    />
                  }
                />
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
