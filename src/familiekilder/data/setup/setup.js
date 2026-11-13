'use strict';

const token = document.querySelector('meta[name="setup-token"]').content;
const byId = (id) => document.getElementById(id);
let currentState = null;
let selectedSource = null;
let fileSource = null;
let polling = false;
let busy = false;
let closed = false;
let renderedServices = '';

async function api(path, value) {
  const options = { headers: { 'X-Setup-Token': token }, cache: 'no-store' };
  if (value !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(value);
  }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Steget kunne ikke fullføres.');
  return result;
}

function error(message) {
  byId('error').textContent = message;
  byId('error').hidden = !message;
}

function button(text, className, action) {
  const node = document.createElement('button');
  node.textContent = text;
  node.className = className;
  node.addEventListener('click', action);
  return node;
}

async function perform(path, value) {
  error('');
  try {
    await api(path, value);
    await refresh();
  } catch (exception) {
    error(exception.message);
  }
}

function showStep(number) {
  for (let index = 1; index <= 3; index += 1) {
    byId(`step-${index}`).hidden = index !== number;
  }
  document.querySelectorAll('.step').forEach((step) => {
    const active = Number(step.dataset.step) === number;
    step.classList.toggle('active', active);
    if (active) step.setAttribute('aria-current', 'step');
    else step.removeAttribute('aria-current');
  });
  byId(`title-${number}`).focus();
}

function selectFile(source) {
  fileSource = source;
  byId('client-file').value = '';
  byId('client-file').click();
}

function openLogin(source) {
  selectedSource = source;
  byId('login-title').textContent = `Koble til ${source.label}`;
  byId('consent-label').hidden = source.id === 'gmail';
  byId('consent').checked = false;
  byId('consent').required = source.id !== 'gmail';
  byId('replace-label').hidden = !source.has_session;
  byId('replace').checked = false;
  byId('replace').required = source.has_session;
  byId('login-dialog').showModal();
}

function renderServices(state) {
  const signature = JSON.stringify([state.services, busy]);
  if (signature === renderedServices) return;
  renderedServices = signature;
  const container = byId('services');
  container.replaceChildren();
  const statuses = {
    connected: 'Innlogging lagret',
    ready: 'Klar til å aktiveres',
    setup_required: 'Trenger oppsett',
    login_required: 'Ikke logget inn',
    attention_required: 'Logg inn på nytt',
    unavailable: 'Kan ikke lese status',
  };
  state.services.forEach((source) => {
    const row = document.createElement('article');
    row.className = 'service';
    const top = document.createElement('div');
    top.className = 'service-top';
    const info = document.createElement('div');
    const heading = document.createElement('h2');
    heading.textContent = source.label;
    const description = document.createElement('p');
    description.className = 'service-description';
    description.textContent = source.description;
    info.append(heading, description);
    const controls = document.createElement('div');
    controls.className = 'service-controls';
    const status = document.createElement('span');
    const effective = source.state === 'connected' && !source.enabled ? 'ready' : source.state;
    status.className = `status ${effective}`;
    status.textContent =
      effective === 'setup_required' && source.automatic_setup
        ? 'Klar til innlogging'
        : statuses[effective];
    let action;
    if (effective === 'connected') {
      action = button('Slå av', 'secondary', () =>
        perform('/api/service', { source: source.id, enabled: false }),
      );
    } else if (effective === 'ready') {
      action = button('Bruk tjenesten', 'secondary', () =>
        perform('/api/service', { source: source.id, enabled: true }),
      );
    } else if (effective === 'setup_required' && !source.automatic_setup) {
      action = button('Legg til oppsett', 'secondary', () => selectFile(source.id));
    } else {
      action = button('Logg inn', 'secondary', () => openLogin(source));
    }
    action.disabled = busy;
    controls.append(status, action);
    top.append(info, controls);
    row.append(top);
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = 'Oppsett og innlogging';
    const help = document.createElement('p');
    help.textContent = source.automatic_setup
      ? 'Du trenger ingen oppsettsfil. Nettklientoppsettet lages lokalt når du velger «Logg inn».'
      : source.id === 'gmail'
        ? 'Gmail trenger en egen Google-klient av typen Desktop app. Velg JSON-filen du lastet ned fra Google.'
        : 'Du kan legge til en klientfil hvis du har en. Filen behandles lokalt og sendes ikke til AI-assistenten.';
    const actions = document.createElement('div');
    actions.className = 'actions';
    const upload = button('Velg oppsettsfil', 'secondary', () => selectFile(source.id));
    upload.disabled = busy;
    actions.append(upload);
    if (source.id === 'ist-home') {
      const auto = button('Hent fra IST Home-appen', 'secondary', () =>
        perform('/api/start', { action: 'ist-client' }),
      );
      auto.disabled = busy;
      actions.append(auto);
    }
    if (source.has_session) {
      const login = button('Logg inn på nytt', 'secondary', () => openLogin(source));
      login.disabled = busy;
      actions.append(login);
    }
    const link = document.createElement('a');
    link.href = '/help/client';
    link.target = '_blank';
    link.rel = 'noreferrer';
    link.textContent = 'Hvordan skaffer jeg oppsettsfilen?';
    details.append(summary, help, actions, document.createElement('p'));
    details.lastChild.append(link);
    row.append(details);
    container.append(row);
  });
}

function render(state) {
  currentState = state;
  busy = !!state.job && ['working', 'waiting'].includes(state.job.phase);
  byId('version').textContent = `familiekilder-mcp ${state.version}`;
  renderServices(state);
  const count = state.services.filter(
    (source) => source.enabled && source.state === 'connected',
  ).length;
  byId('connected-count').textContent =
    count === 1 ? '1 tjeneste er koblet til' : `${count} tjenester er koblet til`;
  const activeJob = state.job && !['cancelled'].includes(state.job.phase);
  byId('job').hidden = !activeJob;
  if (activeJob) {
    byId('job-message').textContent = state.job.message;
    byId('continue-job').hidden = state.job.phase !== 'waiting';
    byId('cancel-job').hidden = !busy;
    byId('job').classList.toggle('error', state.job.phase === 'error');
  }
  const dependencies = byId('dependencies');
  dependencies.replaceChildren();
  [
    ['brave', 'Brave'],
    ['pdf', 'PDF-lesing'],
    ['word', 'Word-lesing'],
  ].forEach(([key, name]) => {
    const item = document.createElement('li');
    item.textContent = `${name}: ${state.dependencies[key] ? 'klart' : 'ikke funnet'}`;
    dependencies.append(item);
  });
  for (const client of state.clients) {
    byId(`${client.id}-status`).textContent = client.error
      ? 'Konfigurasjonen må kontrolleres.'
      : client.configured
        ? 'Koblingen er lagt til.'
        : 'Ikke koblet til ennå.';
    const node = document.querySelector(`[data-client="${client.id}"]`);
    node.disabled = busy;
    node.textContent = client.configured
      ? 'Oppdater koblingen'
      : client.id === 'claude'
        ? 'Koble til Claude Desktop'
        : 'Koble til lokal ChatGPT / Codex';
  }
  byId('test').disabled = busy;
  byId('test-result').hidden = !state.tested;
}

async function refresh() {
  if (polling || closed) return;
  polling = true;
  try {
    render(await api('/api/state'));
  } catch (exception) {
    error('Oppsettet svarer ikke. Start familiekilder-mcp setup på nytt hvis vinduet er lukket.');
  } finally {
    polling = false;
  }
}

async function closeSetup() {
  if (busy && !window.confirm('Avbryte det pågående steget og avslutte oppsettet?')) return;
  try {
    await api('/api/close', {});
    closed = true;
    byId('main').replaceChildren();
    const heading = document.createElement('h1');
    heading.textContent = 'Oppsettet er avsluttet.';
    const text = document.createElement('p');
    text.textContent = 'Du kan lukke fanen. AI-klienten starter MCP-serveren når du bruker den.';
    byId('main').append(heading, text);
    document.querySelectorAll('.sidebar button').forEach((node) => {
      node.disabled = true;
    });
  } catch (exception) {
    error(exception.message);
  }
}

document
  .querySelectorAll('[data-step]')
  .forEach((node) => node.addEventListener('click', () => showStep(Number(node.dataset.step))));
document
  .querySelectorAll('[data-next]')
  .forEach((node) => node.addEventListener('click', () => showStep(Number(node.dataset.next))));
document
  .querySelectorAll('.connect')
  .forEach((node) =>
    node.addEventListener('click', () => perform('/api/connect', { client: node.dataset.client })),
  );
byId('test').addEventListener('click', () => perform('/api/start', { action: 'check' }));
byId('close').addEventListener('click', closeSetup);
byId('finish').addEventListener('click', closeSetup);
byId('dismiss-login').addEventListener('click', () => byId('login-dialog').close());
byId('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  byId('login-dialog').close();
  await perform('/api/start', {
    action: 'login',
    source: selectedSource.id,
    consent: byId('consent').checked,
    replace: byId('replace').checked,
  });
});
byId('continue-job').addEventListener('click', () =>
  perform('/api/job', { id: currentState.job.id, action: 'continue' }),
);
byId('cancel-job').addEventListener('click', () =>
  perform('/api/job', { id: currentState.job.id, action: 'cancel' }),
);
byId('client-file').addEventListener('change', async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  if (file.size > 1024 * 1024) {
    error('Oppsettsfilen er for stor. Velg en JSON-fil under 1 MB.');
    return;
  }
  try {
    const content = await file.text();
    await perform('/api/client-file', { source: fileSource, content });
  } catch (exception) {
    error('Filen kunne ikke leses. Prøv å velge den på nytt.');
  }
});
refresh();
setInterval(refresh, 1200);
