export type SkillEntry = {
  name: string;
  display_name: string;
  summary: string;
  description: string;
  enabled: boolean;
  available: boolean;
  unavailable_reason?: string;
  managed: boolean;
  revision: number;
  policy_revision: number;
};

export type SkillPreview = {
  id: string;
  status: string;
  preview: {
    name: string;
    description: string;
    content: string;
    missing_metadata: string[];
    compatible: boolean;
    compatibility: string;
    compatibility_issues: string[];
    local_changes: boolean;
    total_bytes: number;
    resources: { path: string; size: number; binary: boolean }[];
  };
};
