import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { ask, message, save } from '@tauri-apps/plugin-dialog';

import type {
  ConfigStep,
  ExportFormat,
  ProviderInfo,
  RemoteInfo,
  ScanProgress,
  ScanRequest,
  ScanResult,
} from './types';

/** The same UI runs in the Tauri app and in the Python `gui` web server. */
export interface Backend {
  readonly kind: 'desktop' | 'web';
  version(): Promise<string>;
  listRemotes(): Promise<RemoteInfo[]>;
  listProviders(): Promise<ProviderInfo[]>;
  deleteRemote(name: string): Promise<void>;
  configStart(name: string, provider: string): Promise<ConfigStep>;
  configAnswer(name: string, stepState: string, result: string, isPassword: boolean): Promise<ConfigStep>;
  configCancel(): Promise<void>;
  startScan(request: ScanRequest, onProgress: (p: ScanProgress) => void): Promise<ScanResult>;
  cancelScan(): Promise<void>;
  /** Returns false if the user dismissed the save dialog. */
  exportReport(format: ExportFormat): Promise<boolean>;
  confirm(text: string): Promise<boolean>;
  alert(text: string): Promise<void>;
}

const EXT: Record<ExportFormat, string> = { html: 'html', csv: 'csv', json: 'json' };

function errorText(e: unknown): string {
  if (typeof e === 'string') {
    return e;
  }
  if (e instanceof Error) {
    return e.message;
  }
  return JSON.stringify(e);
}

class DesktopBackend implements Backend {
  readonly kind = 'desktop' as const;

  private call<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
    return invoke<T>(cmd, args).catch((e: unknown) => {
      throw new Error(errorText(e));
    });
  }

  version() { return this.call<string>('rclone_version'); }
  listRemotes() { return this.call<RemoteInfo[]>('list_remotes'); }
  listProviders() { return this.call<ProviderInfo[]>('list_providers'); }
  deleteRemote(name: string) { return this.call<void>('delete_remote', { name }); }
  configStart(name: string, provider: string) { return this.call<ConfigStep>('config_start', { name, provider }); }
  configAnswer(name: string, stepState: string, result: string, isPassword: boolean) {
    return this.call<ConfigStep>('config_answer', { name, stepState, result, isPassword });
  }
  configCancel() { return this.call<void>('config_cancel'); }
  cancelScan() { return this.call<void>('cancel_scan'); }

  async startScan(request: ScanRequest, onProgress: (p: ScanProgress) => void): Promise<ScanResult> {
    const unlisten = await listen<ScanProgress>('scan-progress', (e) => onProgress(e.payload));
    try {
      return await this.call<ScanResult>('start_scan', { request });
    } finally {
      unlisten();
    }
  }

  async exportReport(format: ExportFormat): Promise<boolean> {
    const stamp = new Date().toISOString().slice(0, 10);
    const path = await save({
      defaultPath: `clouddupe-report-${stamp}.${EXT[format]}`,
      filters: [{ name: format.toUpperCase(), extensions: [EXT[format]] }],
    });
    if (path === null) {
      return false;
    }
    await this.call<void>('export_report', { format, path });
    return true;
  }

  confirm(text: string) { return ask(text, { title: 'Cloud Dupe', kind: 'warning' }); }
  async alert(text: string) { await message(text, { title: 'Cloud Dupe' }); }
}

/** Talks to `clouddupe.py gui` on 127.0.0.1 using the token from the URL. */
class WebBackend implements Backend {
  readonly kind = 'web' as const;
  private readonly token: string;

  constructor() {
    const fromHash = new URLSearchParams(location.hash.slice(1)).get('token');
    if (fromHash !== null) {
      sessionStorage.setItem('clouddupe-token', fromHash);
      history.replaceState(null, '', location.pathname);
    }
    this.token = sessionStorage.getItem('clouddupe-token') ?? '';
  }

  private async call<T>(name: string, body: Record<string, unknown> = {}): Promise<T> {
    const res = await fetch(`api/${name}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Clouddupe-Token': this.token },
      body: JSON.stringify(body),
    });
    const data = (await res.json().catch(() => ({}))) as { ok?: boolean; result?: T; error?: string };
    if (!res.ok || data.ok !== true) {
      throw new Error(data.error ?? `request failed (${res.status})`);
    }
    return data.result as T;
  }

  version() { return this.call<string>('rclone_version'); }
  listRemotes() { return this.call<RemoteInfo[]>('list_remotes'); }
  listProviders() { return this.call<ProviderInfo[]>('list_providers'); }
  deleteRemote(name: string) { return this.call<void>('delete_remote', { name }); }
  configStart(name: string, provider: string) { return this.call<ConfigStep>('config_start', { name, provider }); }
  configAnswer(name: string, stepState: string, result: string, isPassword: boolean) {
    return this.call<ConfigStep>('config_answer', { name, stepState, result, isPassword });
  }
  configCancel() { return this.call<void>('config_cancel'); }
  cancelScan() { return this.call<void>('cancel_scan'); }

  async startScan(request: ScanRequest, onProgress: (p: ScanProgress) => void): Promise<ScanResult> {
    await this.call<void>('start_scan', { request });
    let seen = 0;
    for (;;) {
      await new Promise((r) => setTimeout(r, 400));
      const poll = await this.call<{ events: ScanProgress[]; done: boolean; result?: ScanResult; error?: string }>(
        'scan_poll',
        { since: seen },
      );
      poll.events.forEach(onProgress);
      seen += poll.events.length;
      if (poll.done) {
        if (poll.error !== undefined && poll.error !== null) {
          throw new Error(poll.error);
        }
        return poll.result as ScanResult;
      }
    }
  }

  async exportReport(format: ExportFormat): Promise<boolean> {
    const res = await fetch(`api/export?format=${format}`, { headers: { 'X-Clouddupe-Token': this.token } });
    if (!res.ok) {
      const data = (await res.json().catch(() => ({}))) as { error?: string };
      throw new Error(data.error ?? `export failed (${res.status})`);
    }
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement('a');
    a.href = url;
    a.download = `clouddupe-report-${new Date().toISOString().slice(0, 10)}.${EXT[format]}`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
    return true;
  }

  async confirm(text: string) { return window.confirm(text); }
  async alert(text: string) { window.alert(text); }
}

export function createBackend(): Backend {
  return '__TAURI_INTERNALS__' in window ? new DesktopBackend() : new WebBackend();
}
