/** Shape of the APIResponse objects returned by sn-plugin-lib (typed as `Object` in its d.ts). */
export type ApiResult<T> = {
  success: boolean;
  result?: T | null;
  error?: {code: number; message: string} | null;
};

export function asResult<T>(value: unknown): ApiResult<T> {
  if (value && typeof value === 'object' && 'success' in value) {
    return value as ApiResult<T>;
  }
  return {success: false, error: {code: -1, message: 'Empty response'}};
}
