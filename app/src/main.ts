import './styles.css';

import { createBackend } from './api';
import { h, humanise, humanSize, parseSize } from './dom';
import type {
  Confidence,
  ConfigStep,
  ExportFormat,
  Group,
  ProviderInfo,
  RemoteInfo,
  ScanProgress,
  ScanResult,
} from './types';

const api = createBackend();

const FEATURED = ['dropbox', 'pikpak', 'drive', 'onedrive', 'box', 'pcloud', 'mega', 'yandex'];
const PAGE = 200;

const state = {
  version: '',
  remotes: [] as RemoteInfo[],
  remotesError: '',
  selected: new Map<string, string>(),
  minSize: '1',
  sizeOnly: false,
  sizeOnlyMin: '50M',
  scanning: false,
  progress: new Map<string, ScanProgress>(),
  result: null as ScanResult | null,
  scanError: '',
  search: '',
  confidence: '' as '' | Confidence,
  shown: PAGE,
};

const root = document.getElementById('app') as HTMLElement;
const drivesEl = h('div', { class: 'drives' });
const optionsEl = h('section', { class: 'panel' });
const progressEl = h('section', { class: 'progress' });
const resultsEl = h('section', { class: 'results' });

root.append(
  h(
    'div',
    { class: 'shell' },
    h(
      'aside',
      { class: 'sidebar' },
      h('div', { class: 'brand' }, h('span', { class: 'logo', 'aria-hidden': 'true' }), 'Cloud Dupe'),
      h('h2', {}, 'Drives'),
      drivesEl,
      h('button', { class: 'btn ghost full', onclick: () => void openWizard() }, '+ Add drive'),
      h(
        'p',
        { class: 'fine' },
        'Files are compared using hashes your provider already stores. Nothing is downloaded or changed.',
      ),
    ),
    h('main', { class: 'main' }, optionsEl, progressEl, resultsEl),
  ),
);

// ---------------------------------------------------------------------------
// Drives
// ---------------------------------------------------------------------------

async function loadRemotes(): Promise<void> {
  try {
    state.remotes = await api.listRemotes();
    state.remotesError = '';
    for (const name of [...state.selected.keys()]) {
      if (!state.remotes.some((r) => r.name === name)) {
        state.selected.delete(name);
      }
    }
  } catch (e) {
    state.remotesError = (e as Error).message;
  }
  renderDrives();
  renderOptions();
}

function renderDrives(): void {
  drivesEl.replaceChildren();
  if (state.remotesError !== '') {
    drivesEl.append(h('p', { class: 'error' }, state.remotesError));
    return;
  }
  if (state.remotes.length === 0) {
    drivesEl.append(h('p', { class: 'muted' }, 'No drives yet. Add one to get started.'));
    return;
  }
  for (const r of state.remotes) {
    const checked = state.selected.has(r.name);
    const folder = h('input', {
      class: 'folder',
      placeholder: 'Folder (optional, whole drive if empty)',
      value: state.selected.get(r.name) ?? '',
      oninput: (e: Event) => state.selected.set(r.name, (e.target as HTMLInputElement).value),
    });
    drivesEl.append(
      h(
        'div',
        { class: checked ? 'drive on' : 'drive' },
        h(
          'label',
          { class: 'drive-row' },
          h('input', {
            type: 'checkbox',
            checked,
            disabled: state.scanning,
            onchange: (e: Event) => {
              if ((e.target as HTMLInputElement).checked) {
                state.selected.set(r.name, '');
              } else {
                state.selected.delete(r.name);
              }
              renderDrives();
              renderOptions();
            },
          }),
          h('span', { class: 'drive-name' }, r.name),
          h('span', { class: 'chip' }, r.type),
          h(
            'button',
            {
              class: 'icon-btn',
              title: `Remove ${r.name}`,
              'aria-label': `Remove ${r.name}`,
              disabled: state.scanning,
              onclick: (e: Event) => {
                e.preventDefault();
                void removeRemote(r.name);
              },
            },
            '×',
          ),
        ),
        checked ? folder : null,
      ),
    );
  }
}

async function removeRemote(name: string): Promise<void> {
  const ok = await api.confirm(
    `Remove "${name}" from Cloud Dupe?\n\nThis only forgets the sign-in on this computer. No files are deleted.`,
  );
  if (!ok) {
    return;
  }
  try {
    await api.deleteRemote(name);
  } catch (e) {
    await api.alert((e as Error).message);
  }
  await loadRemotes();
}

// ---------------------------------------------------------------------------
// Options and scanning
// ---------------------------------------------------------------------------

function renderOptions(): void {
  const count = state.selected.size;
  const field = (label: string, input: HTMLElement, hint: string) =>
    h('label', { class: 'field' }, h('span', {}, label), input, h('small', {}, hint));

  optionsEl.replaceChildren(
    h(
      'div',
      { class: 'panel-head' },
      h('h1', {}, 'Find duplicates'),
      h('span', { class: 'muted' }, state.version),
    ),
    h(
      'div',
      { class: 'fields' },
      field(
        'Ignore files smaller than',
        h('input', {
          value: state.minSize,
          disabled: state.scanning,
          oninput: (e: Event) => { state.minSize = (e.target as HTMLInputElement).value; },
        }),
        'e.g. 1, 100K, 5M. 1 skips empty files.',
      ),
      h(
        'label',
        { class: 'field check' },
        h('input', {
          type: 'checkbox',
          checked: state.sizeOnly,
          disabled: state.scanning,
          onchange: (e: Event) => {
            state.sizeOnly = (e.target as HTMLInputElement).checked;
            renderOptions();
          },
        }),
        h('span', {}, 'Also flag same-size files with different names'),
        h('small', {}, 'Catches renamed copies when no hash can be compared. More false positives.'),
      ),
      state.sizeOnly
        ? field(
            'Only for files of at least',
            h('input', {
              value: state.sizeOnlyMin,
              disabled: state.scanning,
              oninput: (e: Event) => { state.sizeOnlyMin = (e.target as HTMLInputElement).value; },
            }),
            'Larger files make size-only matches more meaningful.',
          )
        : null,
    ),
    h(
      'div',
      { class: 'actions' },
      state.scanning
        ? h('button', { class: 'btn danger', onclick: () => void api.cancelScan() }, 'Cancel scan')
        : h(
            'button',
            { class: 'btn primary', disabled: count === 0, onclick: () => void runScan() },
            count === 0 ? 'Select a drive to scan' : `Scan ${count} drive${count === 1 ? '' : 's'}`,
          ),
    ),
  );
}

async function runScan(): Promise<void> {
  const minSize = parseSize(state.minSize);
  const sizeOnlyMin = parseSize(state.sizeOnlyMin);
  if (minSize === null || (state.sizeOnly && sizeOnlyMin === null)) {
    await api.alert('Sizes must look like 0, 512, 100K, 50M or 2G.');
    return;
  }
  const targets = [...state.selected.entries()].map(([name, folder]) => {
    const clean = folder.trim().replace(/^\/+/, '');
    return `${name}:${clean}`;
  });

  state.scanning = true;
  state.scanError = '';
  state.result = null;
  state.progress = new Map(targets.map((t) => [t, { target: t, files: 0, done: false, error: null }]));
  renderDrives();
  renderOptions();
  renderProgress();
  renderResults();

  try {
    state.result = await api.startScan(
      { targets, minSize, sizeOnly: state.sizeOnly, sizeOnlyMin: sizeOnlyMin ?? 0 },
      (p) => {
        state.progress.set(p.target, p);
        renderProgress();
      },
    );
    state.shown = PAGE;
  } catch (e) {
    const msg = (e as Error).message;
    state.scanError = msg === 'cancelled' ? 'Scan cancelled.' : msg;
  } finally {
    state.scanning = false;
    renderDrives();
    renderOptions();
    renderProgress();
    renderResults();
  }
}

function renderProgress(): void {
  progressEl.replaceChildren();
  if (!state.scanning && state.scanError === '') {
    return;
  }
  if (state.scanError !== '') {
    progressEl.append(h('p', { class: 'error' }, state.scanError));
    return;
  }
  progressEl.append(
    h('h2', {}, 'Listing drives…'),
    ...[...state.progress.values()].map((p) =>
      h(
        'div',
        { class: 'prog-row' },
        h('span', { class: 'path' }, p.target),
        h(
          'span',
          { class: p.error !== null ? 'error' : p.done ? 'ok' : 'muted' },
          p.error !== null
            ? `failed: ${p.error}`
            : `${p.files.toLocaleString()} files${p.done ? ' ✓' : '…'}`,
        ),
        p.done ? null : h('span', { class: 'spinner', 'aria-hidden': 'true' }),
      ),
    ),
  );
}

// ---------------------------------------------------------------------------
// Results
// ---------------------------------------------------------------------------

function wasted(g: Group): number {
  return Math.max(g.members[0]?.size ?? 0, 0) * (g.members.length - 1);
}

function fullPath(remote: string, path: string): string {
  return remote.endsWith(':') || remote.endsWith('/') ? remote + path : `${remote}/${path}`;
}

function filteredGroups(r: ScanResult): Group[] {
  const term = state.search.trim().toLowerCase();
  return r.groups.filter((g) => {
    if (state.confidence !== '' && g.confidence !== state.confidence) {
      return false;
    }
    if (term === '') {
      return true;
    }
    return (
      g.id.toLowerCase().includes(term) ||
      g.members.some((m) => fullPath(m.remote, m.path).toLowerCase().includes(term))
    );
  });
}

async function doExport(format: ExportFormat): Promise<void> {
  try {
    await api.exportReport(format);
  } catch (e) {
    await api.alert((e as Error).message);
  }
}

function renderResults(): void {
  resultsEl.replaceChildren();
  const r = state.result;
  if (r === null) {
    if (!state.scanning && state.scanError === '') {
      resultsEl.append(
        h(
          'div',
          { class: 'empty' },
          h('h2', {}, 'How it works'),
          h(
            'ul',
            {},
            h('li', {}, h('b', {}, 'Confirmed'), ': same size and same content hash. Identical, whatever the name.'),
            h('li', {}, h('b', {}, 'Probable'), ': same size and name, but the drives use different hash types.'),
            h('li', {}, h('b', {}, 'Possible'), ': same size only (optional). Worth checking by hand.'),
          ),
          h('p', { class: 'muted' }, 'Pick one or more drives on the left, then scan.'),
        ),
      );
    }
    return;
  }

  const s = r.summary;
  const cards = [h('div', { class: 'card' }, h('span', { class: 'muted' }, 'Files scanned'), h('b', {}, s.total_files.toLocaleString()))];
  for (const c of ['confirmed', 'probable', 'possible'] as Confidence[]) {
    const stats = s.by_confidence[c];
    if (stats !== undefined) {
      cards.push(
        h(
          'div',
          { class: 'card' },
          h('span', { class: `tag ${c}` }, c),
          h('b', {}, `${stats.groups.toLocaleString()} groups`),
          h('span', { class: 'muted' }, `${humanSize(stats.wasted_bytes)} reclaimable`),
        ),
      );
    }
  }

  const notes = s.comparability
    .filter((p) => p.common_hash_types.length === 0)
    .map((p) => h('li', {}, `${p.a} and ${p.b} share no hash type, so matches between them can only be probable.`));

  const coverage = h(
    'details',
    { class: 'coverage' },
    h('summary', {}, 'Hash coverage by drive'),
    h(
      'table',
      {},
      h('tr', {}, h('th', {}, 'Drive'), h('th', {}, 'Files'), h('th', {}, 'Size'), h('th', {}, 'With hash'), h('th', {}, 'Hash types')),
      ...Object.entries(s.remotes).map(([name, v]) =>
        h(
          'tr',
          {},
          h('td', { class: 'path' }, name),
          h('td', {}, v.files.toLocaleString()),
          h('td', {}, humanSize(v.bytes)),
          h('td', {}, `${(v.hash_coverage * 100).toFixed(1)}%`),
          h('td', {}, v.hash_types.join(', ') || 'none'),
        ),
      ),
    ),
  );

  const list = h('div', { class: 'groups' });
  const renderList = () => {
    const groups = filteredGroups(r);
    list.replaceChildren(
      h('p', { class: 'muted' }, `${Math.min(state.shown, groups.length).toLocaleString()} of ${groups.length.toLocaleString()} groups shown`),
      ...groups.slice(0, state.shown).map(renderGroup),
    );
    if (groups.length > state.shown) {
      list.append(
        h('button', { class: 'btn ghost', onclick: () => { state.shown += PAGE; renderList(); } }, 'Show more'),
      );
    }
    if (groups.length === 0) {
      list.append(h('p', { class: 'muted' }, r.groups.length === 0 ? 'No duplicates found.' : 'No groups match the filter.'));
    }
  };

  const sections: (HTMLElement | null)[] = [
    h('div', { class: 'cards' }, ...cards),
    s.errors.length > 0
      ? h('div', { class: 'notice error' }, h('b', {}, 'Some drives failed'), h('ul', {}, ...s.errors.map((e) => h('li', {}, e))))
      : null,
    notes.length > 0 ? h('div', { class: 'notice' }, h('ul', {}, ...notes)) : null,
    coverage,
    h(
      'div',
      { class: 'toolbar' },
      h('input', {
        type: 'search',
        placeholder: 'Filter by path, drive or group ID',
        value: state.search,
        oninput: (e: Event) => {
          state.search = (e.target as HTMLInputElement).value;
          state.shown = PAGE;
          renderList();
        },
      }),
      h(
        'select',
        {
          onchange: (e: Event) => {
            state.confidence = (e.target as HTMLSelectElement).value as '' | Confidence;
            state.shown = PAGE;
            renderList();
          },
        },
        ...[['', 'All levels'], ['confirmed', 'Confirmed'], ['probable', 'Probable'], ['possible', 'Possible']].map(
          ([v, label]) => h('option', { value: v, selected: state.confidence === v }, label),
        ),
      ),
      h(
        'div',
        { class: 'export' },
        h('span', { class: 'muted' }, 'Export'),
        ...(['html', 'csv', 'json'] as ExportFormat[]).map((f) =>
          h('button', { class: 'btn ghost small', onclick: () => void doExport(f) }, f.toUpperCase()),
        ),
      ),
    ),
    list,
  ];
  resultsEl.append(...sections.filter((n): n is HTMLElement => n !== null));
  renderList();
}

function renderGroup(g: Group): HTMLElement {
  const first = g.members[0];
  const name = first.path.split('/').pop() ?? first.path;
  return h(
    'details',
    { class: 'group' },
    h(
      'summary',
      {},
      h('span', { class: `tag ${g.confidence}` }, g.confidence),
      h('b', { class: 'gname' }, name),
      h(
        'span',
        { class: 'muted' },
        `${g.members.length} copies · ${humanSize(first.size)} each · ${humanSize(wasted(g))} reclaimable · matched on ${g.evidence.join(' + ')} · ${g.id}`,
      ),
    ),
    ...g.warnings.map((w) => h('p', { class: 'error' }, w)),
    h(
      'table',
      {},
      h('tr', {}, h('th', {}, 'Location'), h('th', {}, 'Modified'), h('th', {}, 'Hashes')),
      ...g.members.map((m) =>
        h(
          'tr',
          {},
          h('td', { class: 'path' }, fullPath(m.remote, m.path)),
          h('td', {}, m.mod_time.slice(0, 19).replace('T', ' ')),
          h(
            'td',
            { class: 'path' },
            Object.entries(m.hashes).map(([k, v]) => `${k}:${v.slice(0, 12)}`).join(', ') || '-',
          ),
        ),
      ),
    ),
  );
}

// ---------------------------------------------------------------------------
// Add-drive wizard
// ---------------------------------------------------------------------------

let providers: ProviderInfo[] | null = null;

async function openWizard(): Promise<void> {
  const dialog = h('dialog', { class: 'wizard' });
  document.body.append(dialog);
  const body = h('div', { class: 'wizard-body' });
  dialog.append(body);
  dialog.showModal();

  let remoteName = '';
  let created = false;
  let busy = false;

  const close = async (discard: boolean) => {
    if (busy) {
      await api.configCancel();
    }
    if (discard && created) {
      await api.deleteRemote(remoteName).catch(() => undefined);
    }
    dialog.close();
    dialog.remove();
    await loadRemotes();
  };

  dialog.addEventListener('cancel', (e) => {
    e.preventDefault();
    void close(true);
  });

  const setBody = (...nodes: (Node | null)[]) => body.replaceChildren(...nodes.filter((n): n is Node => n !== null));

  const waiting = (text: string, cancellable: boolean) => {
    setBody(
      h('div', { class: 'waiting' }, h('span', { class: 'spinner' }), h('p', {}, text)),
      cancellable ? h('div', { class: 'actions' }, h('button', { class: 'btn ghost', onclick: () => void close(true) }, 'Cancel')) : null,
    );
  };

  const fail = (msg: string) => {
    busy = false;
    setBody(
      h('h2', {}, 'Could not add the drive'),
      h('p', { class: 'error' }, msg),
      h(
        'div',
        { class: 'actions' },
        h('button', { class: 'btn ghost', onclick: () => void close(true) }, 'Close'),
        h(
          'button',
          {
            class: 'btn primary',
            onclick: async () => {
              if (created) {
                await api.deleteRemote(remoteName).catch(() => undefined);
                created = false;
              }
              void chooseProvider();
            },
          },
          'Start again',
        ),
      ),
    );
  };

  const run = async (work: () => Promise<ConfigStep>, waitText: string) => {
    busy = true;
    waiting(waitText, true);
    try {
      const step = await work();
      busy = false;
      showStep(step);
    } catch (e) {
      const msg = (e as Error).message;
      if (msg !== 'cancelled') {
        fail(msg);
      }
    }
  };

  const showStep = (step: ConfigStep) => {
    if (step.done) {
      state.selected.set(remoteName, '');
      void close(false);
      return;
    }
    const q = step.question;
    if (q === null) {
      fail('rclone returned an unexpected question.');
      return;
    }
    if (step.oauth) {
      setBody(
        h('h2', {}, `Sign in to connect "${remoteName}"`),
        h('p', {}, 'Your web browser will open so you can sign in and allow access. Come back here when it says the sign-in succeeded.'),
        step.error !== '' ? h('p', { class: 'error' }, step.error) : null,
        h(
          'div',
          { class: 'actions' },
          h('button', { class: 'btn ghost', onclick: () => void close(true) }, 'Cancel'),
          h(
            'button',
            {
              class: 'btn primary',
              onclick: () => void run(
                () => api.configAnswer(remoteName, step.state, 'true', false),
                'Waiting for you to finish signing in in your browser…',
              ),
            },
            'Open browser to sign in',
          ),
        ),
      );
      return;
    }

    let input: HTMLInputElement | HTMLSelectElement;
    const isBool = q.kind === 'bool';
    if ((q.exclusive && q.examples.length > 0) || isBool) {
      const choices = q.examples.length > 0 ? q.examples : [{ value: 'true', help: 'Yes' }, { value: 'false', help: 'No' }];
      input = h(
        'select',
        {},
        ...choices.map((c) =>
          h('option', { value: c.value, selected: c.value === q.default }, c.help !== '' ? `${c.value} — ${c.help.split('\n')[0]}` : c.value),
        ),
      );
    } else {
      const listId = `opts-${Math.random().toString(36).slice(2)}`;
      input = h('input', {
        type: q.isPassword ? 'password' : 'text',
        value: q.isPassword ? '' : q.default,
        list: q.examples.length > 0 ? listId : null,
        autocomplete: 'off',
        spellcheck: 'false',
      });
      if (q.examples.length > 0) {
        body.append(h('datalist', { id: listId }, ...q.examples.map((c) => h('option', { value: c.value }, c.help))));
      }
    }
    const err = h('p', { class: 'error' }, step.error);

    const submit = () => {
      const value = input.value;
      if (q.required && value.trim() === '') {
        err.textContent = 'This value is required.';
        return;
      }
      void run(() => api.configAnswer(remoteName, step.state, value, q.isPassword), 'Saving…');
    };

    const form = h(
      'form',
      {
        onsubmit: (e: Event) => {
          e.preventDefault();
          submit();
        },
      },
      h('h2', {}, `Set up "${remoteName}"`),
      h('label', { class: 'field' }, h('span', {}, humanise(q.name), q.required ? ' *' : ''), input),
      h('pre', { class: 'help' }, q.help),
      err,
      h(
        'div',
        { class: 'actions' },
        h('button', { type: 'button', class: 'btn ghost', onclick: () => void close(true) }, 'Cancel'),
        h('button', { type: 'submit', class: 'btn primary' }, 'Next'),
      ),
    );
    const datalist = body.querySelector('datalist');
    setBody(form, datalist);
    input.focus();
  };

  const chooseProvider = async () => {
    if (providers === null) {
      waiting('Loading providers…', false);
      try {
        providers = await api.listProviders();
      } catch (e) {
        fail((e as Error).message);
        return;
      }
    }
    const all = providers;
    let picked = '';
    const list = h('div', { class: 'provider-list', role: 'listbox' });
    const nameInput = h('input', { placeholder: 'e.g. dropbox', autocomplete: 'off', spellcheck: 'false' });
    const err = h('p', { class: 'error' });
    const search = h('input', { type: 'search', placeholder: 'Search providers' });

    const renderList = () => {
      const term = search.value.trim().toLowerCase();
      const matches = all.filter(
        (p) => term === '' || p.name.includes(term) || p.description.toLowerCase().includes(term),
      );
      const featured = matches.filter((p) => FEATURED.includes(p.name));
      const rest = matches.filter((p) => !FEATURED.includes(p.name));
      const item = (p: ProviderInfo) =>
        h(
          'button',
          {
            type: 'button',
            class: p.name === picked ? 'provider picked' : 'provider',
            role: 'option',
            onclick: () => {
              picked = p.name;
              if (nameInput.value.trim() === '') {
                nameInput.value = p.name;
              }
              renderList();
            },
          },
          h('b', {}, p.description),
          h('span', { class: 'muted' }, p.name),
        );
      list.replaceChildren(
        ...(featured.length > 0 ? [h('div', { class: 'list-label' }, 'Popular'), ...featured.map(item)] : []),
        ...(rest.length > 0 ? [h('div', { class: 'list-label' }, 'All providers'), ...rest.map(item)] : []),
      );
    };
    search.addEventListener('input', renderList);
    renderList();

    setBody(
      h(
        'form',
        {
          onsubmit: (e: Event) => {
            e.preventDefault();
            const name = nameInput.value.trim();
            if (picked === '') {
              err.textContent = 'Choose a provider.';
              return;
            }
            if (!/^[\p{L}\p{N}_.+@][\p{L}\p{N}_.+@ -]*$/u.test(name) || name.endsWith(' ')) {
              err.textContent = 'Name: letters, numbers, spaces and _ - . + @ only, not starting with - or a space.';
              return;
            }
            if (state.remotes.some((r) => r.name === name)) {
              err.textContent = `A drive called "${name}" already exists.`;
              return;
            }
            remoteName = name;
            void run(async () => {
              const step = await api.configStart(name, picked);
              created = true;
              return step;
            }, 'Starting setup…');
          },
        },
        h('h2', {}, 'Add a drive'),
        search,
        list,
        h('label', { class: 'field' }, h('span', {}, 'Name for this drive'), nameInput),
        err,
        h(
          'div',
          { class: 'actions' },
          h('button', { type: 'button', class: 'btn ghost', onclick: () => void close(false) }, 'Cancel'),
          h('button', { type: 'submit', class: 'btn primary' }, 'Continue'),
        ),
      ),
    );
    search.focus();
  };

  await chooseProvider();
}

// ---------------------------------------------------------------------------
// Start-up
// ---------------------------------------------------------------------------

async function init(): Promise<void> {
  renderOptions();
  renderResults();
  try {
    state.version = await api.version();
  } catch (e) {
    state.remotesError = (e as Error).message;
  }
  await loadRemotes();
}

void init();
