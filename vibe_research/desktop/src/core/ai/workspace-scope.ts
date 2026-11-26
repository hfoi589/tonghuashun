let currentScope = 'unassigned'

export function setWorkspaceScope(userId: number | string): void {
  const value = String(userId)
  if (!/^\d{1,10}$/.test(value)) throw new TypeError('工作区账号编号无效')
  currentScope = value
}

export function workspaceStorageKey(key: string): string {
  return `vr-user:${currentScope}:${key}`
}
