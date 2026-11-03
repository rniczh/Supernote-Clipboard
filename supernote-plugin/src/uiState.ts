import {useSyncExternalStore} from 'react';

export type Screen = 'shots' | 'phone' | 'settings';

export type Notice = {text: string; error: boolean};

type UiState = {
  screen: Screen;
  notice: Notice | null;
  /** Bumped on every open so the screenshot list reloads. */
  openCount: number;
};

/**
 * Button listeners in index.js run outside React; they write here and the
 * plugin view (App.tsx) renders whatever was requested last.
 */
let state: UiState = {screen: 'shots', notice: null, openCount: 0};
const listeners = new Set<() => void>();

export function openScreen(screen: Screen, notice: Notice | null = null) {
  state = {screen, notice, openCount: state.openCount + 1};
  listeners.forEach(listener => listener());
}

export function setNotice(notice: Notice | null) {
  state = {...state, notice};
  listeners.forEach(listener => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useUiState(): UiState {
  return useSyncExternalStore(subscribe, () => state);
}
