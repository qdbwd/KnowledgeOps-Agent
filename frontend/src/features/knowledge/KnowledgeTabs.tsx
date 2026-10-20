import { NavLink } from 'react-router-dom';

export function KnowledgeTabs() {
  const style =
    'rounded-lg px-4 py-2 text-sm hover:bg-white [&.active]:bg-white [&.active]:font-semibold [&.active]:shadow-sm';
  return (
    <nav aria-label="知识库内容" className="mb-6 flex gap-2 border-b border-line pb-3">
      <NavLink to="/knowledge" end className={style}>
        已发布文章
      </NavLink>
      <NavLink to="/knowledge/reviews" className={style}>
        草稿与审阅
      </NavLink>
    </nav>
  );
}
