/** "config" errors send the user to the Settings screen; "network" ones to the screenshot list. */
export type ErrorKind = 'config' | 'network' | 'other';

/** An error whose message is written for the person holding the Supernote. */
export class UserError extends Error {
  kind: ErrorKind;

  constructor(message: string, kind: ErrorKind = 'other') {
    super(message);
    this.kind = kind;
  }
}

export function toUserError(e: unknown): UserError {
  if (e instanceof UserError) {
    return e;
  }
  const message = e instanceof Error ? e.message : String(e);
  return new UserError(`Something went wrong: ${message}`);
}
