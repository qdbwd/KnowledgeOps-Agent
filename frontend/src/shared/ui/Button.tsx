import type { ComponentProps } from 'react';

/** 统一原生按钮外观；保留 type、ref、disabled 和事件的原生语义。 */
export function Button({ className = '', ...props }: ComponentProps<'button'>) {
  return <button {...props} className={`button ${className}`} />;
}
