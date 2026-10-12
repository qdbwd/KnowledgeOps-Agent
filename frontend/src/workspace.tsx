import { createContext, useContext } from 'react';
export const WorkspaceContext = createContext({ version: 0, refresh: () => {} });
export const useWorkspace = () => useContext(WorkspaceContext);
