/** 展示身份与模型函数名分离；此结构不授予任何执行权限。 */
export type CapabilityChoice = {
  id: string;
  name: string;
  display_name?: string;
  summary?: string;
  description?: string;
  unavailable_reason?: string;
  disabled?: boolean;
  detail_url?: string;
  members?: CapabilityChoice[];
  status?: string;
};

export function capabilityName(choice: CapabilityChoice) {
  return choice.display_name || choice.name;
}
