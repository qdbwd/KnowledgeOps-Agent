"""面向用户的目录摘要，与模型路由描述分开；新增未知包沿用自身元数据。"""

PRESENTATION = {
    "academic-paper-review": ("论文审阅", "分析论文方法、贡献、证据和局限。"),
    "code-documentation": ("代码文档", "编写项目说明、接口文档和开发指南。"),
    "consulting-analysis": ("研究分析报告", "组织行业、市场或专题研究报告。"),
    "deep-research": ("深度研究", "围绕复杂问题检索资料并交叉核对证据。"),
    "evidence-check": ("证据核对", "核对来源、数字、条件和引用是否支持结论。"),
    "frontend-design": ("前端设计", "设计网页与交互界面的结构、视觉和实现。"),
    "github-deep-research": ("GitHub 项目研究", "阅读项目代码与演进资料，形成有依据的分析。"),
    "guided-learning": ("引导学习", "围绕当前问题解释概念、联系实践并逐步深入。"),
    "knowledge-maintenance": ("知识维护", "整理、修订和关联已有知识正文。"),
    "skill-creator": ("技能编写", "把任务方法整理成可复用的技能说明与资源。"),
    "skill-reviewer": ("技能审阅", "检查技能包的内容、结构与使用条件。"),
    "systematic-literature-review": ("系统文献综述", "检索、筛选和归纳研究文献及证据。"),
}


def present_skill(item: dict) -> dict:
    title, summary = PRESENTATION.get(item["name"], (item["name"], item["description"]))
    return {**item, "display_name": title, "summary": summary}
