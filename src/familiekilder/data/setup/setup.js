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
let municipalities = null;

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
  if (message) showInfo('');
}

function showInfo(message) {
  byId('info').textContent = message;
  byId('info').hidden = !message;
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
  showInfo('');
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

function loadMunicipalities() {
  if (!municipalities) {
    municipalities = api('/api/flyt-municipalities').then(
      (result) => {
        const list = byId('municipalities');
        list.replaceChildren();
        // Two entries can share a name; the organisation number tells them apart.
        const counts = new Map();
        result.municipalities.forEach((item) => {
          counts.set(item.name, (counts.get(item.name) || 0) + 1);
        });
        const labelled = result.municipalities.map((item) => ({
          id: item.id,
          label: counts.get(item.name) > 1 ? `${item.name} (${item.id})` : item.name,
        }));
        labelled.forEach((municipality) => {
          const option = document.createElement('option');
          option.value = municipality.label;
          list.append(option);
        });
        return labelled;
      },
      (exception) => {
        municipalities = null;
        throw exception;
      },
    );
  }
  return municipalities;
}

function loginError(message) {
  byId('login-error').textContent = message;
  byId('login-error').hidden = !message;
}

// The municipality can be chosen or changed only while the service has no login.
function needsMunicipality(source) {
  return source.setup === 'municipality' && !source.has_session;
}

// The names the stored municipality can have in the field, in lower case.
function storedMunicipalityLabels(source) {
  const stored = source.municipality;
  if (!stored) return [];
  return [stored.name, `${stored.name} (${stored.id})`].map((label) =>
    label.toLocaleLowerCase('nb'),
  );
}

function openLogin(source) {
  selectedSource = source;
  byId('login-title').textContent = `Koble til ${source.label}`;
  loginError('');
  const municipality = needsMunicipality(source);
  const stored = municipality ? source.municipality : null;
  byId('municipality-field').hidden = !municipality;
  byId('municipality').required = municipality;
  byId('municipality').value = stored ? stored.name : '';
  byId('municipality-hint').textContent = stored
    ? 'Kommunen som er lagret. Er den feil, velger du en annen fra listen før du logger inn.'
    : 'Skriv navnet på kommunen eller den som driver barnehagen, og velg fra listen.';
  if (municipality) {
    loadMunicipalities().then(
      (list) => {
        // Show the stored choice the way the list names it.
        const match = stored && list.find((item) => item.id === stored.id);
        if (match && byId('municipality').value === stored.name) {
          byId('municipality').value = match.label;
        }
      },
      () =>
        loginError('Kommunelisten kunne ikke hentes. Kontroller nettforbindelsen og prøv igjen.'),
    );
  }
  byId('consent-label').hidden = source.id === 'gmail';
  byId('consent').checked = false;
  byId('consent').required = source.id !== 'gmail';
  byId('replace-label').hidden = !source.has_session;
  byId('replace').checked = false;
  byId('replace').required = source.has_session;
  byId('login-dialog').showModal();
}

function serviceAction(source) {
  if (source.state === 'connected' && source.enabled) {
    return {
      status: 'Innlogging lagret',
      label: 'Slå av',
      action: () => perform('/api/service', { source: source.id, enabled: false }),
    };
  }
  if (source.state === 'connected') {
    return {
      status: 'Klar til å aktiveres',
      label: 'Bruk tjenesten',
      action: () => perform('/api/service', { source: source.id, enabled: true }),
    };
  }
  if (source.state === 'setup_required' && source.setup === 'file') {
    return {
      status: 'Trenger Google-fil',
      label: 'Velg Google-fil',
      action: () => selectFile(source.id),
    };
  }
  if (source.state === 'setup_required' && source.setup === 'unavailable') {
    return { status: 'Ikke tilgjengelig i veiviseren ennå', label: null, action: null };
  }
  const statuses = {
    attention_required: 'Logg inn på nytt',
    unavailable: 'Status kunne ikke leses',
  };
  return {
    status: statuses[source.state] || 'Ikke logget inn',
    label: 'Logg inn',
    action: () => openLogin(source),
  };
}

function renderServices(state) {
  const signature = JSON.stringify([state.services, busy]);
  if (signature === renderedServices) return;
  renderedServices = signature;
  const container = byId('services');
  container.replaceChildren();
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
    if (source.setup === 'file') {
      const help = document.createElement('a');
      help.className = 'service-help';
      help.href = '/help/client';
      help.target = '_blank';
      help.rel = 'noreferrer';
      help.textContent = 'Slik lager du Google-filen';
      info.append(help);
    }
    const controls = document.createElement('div');
    controls.className = 'service-controls';
    const choice = serviceAction(source);
    const status = document.createElement('span');
    const ready = source.state === 'connected' && !source.enabled;
    status.className = `status ${ready ? 'ready' : source.state}`;
    status.textContent = choice.status;
    controls.append(status);
    if (choice.label) {
      const action = button(choice.label, 'secondary', choice.action);
      action.disabled = busy;
      controls.append(action);
    }
    if (source.has_session && choice.label !== 'Logg inn') {
      const again = button('Logg inn på nytt', 'text-button', () => openLogin(source));
      again.disabled = busy;
      controls.append(again);
    }
    if (
      source.setup === 'file' &&
      !source.has_session &&
      !['setup_required', 'unavailable'].includes(source.state)
    ) {
      const other = button('Velg en annen Google-fil', 'text-button', () => selectFile(source.id));
      other.disabled = busy;
      controls.append(other);
    }
    top.append(info, controls);
    row.append(top);
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
  const request = {
    action: 'login',
    source: selectedSource.id,
    consent: byId('consent').checked,
    replace: byId('replace').checked,
  };
  const typed = byId('municipality').value.trim().toLocaleLowerCase('nb');
  if (
    needsMunicipality(selectedSource) &&
    storedMunicipalityLabels(selectedSource).includes(typed)
  ) {
    // Unchanged: the service keeps the stored municipality without a lookup.
    request.municipality = selectedSource.municipality.id;
  } else if (needsMunicipality(selectedSource)) {
    let list;
    try {
      list = await loadMunicipalities();
    } catch (exception) {
      loginError('Kommunelisten kunne ikke hentes. Kontroller nettforbindelsen og prøv igjen.');
      return;
    }
    const match = list.find((item) => item.label.toLocaleLowerCase('nb') === typed);
    if (!match) {
      loginError('Fant ikke kommunen. Velg en kommune fra listen.');
      byId('municipality').focus();
      return;
    }
    request.municipality = match.id;
  }
  byId('login-dialog').close();
  await perform('/api/start', request);
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
    error('Filen er for stor. Velg JSON-filen du lastet ned fra Google.');
    return;
  }
  let content;
  try {
    content = await file.text();
  } catch (exception) {
    error('Filen kunne ikke leses. Prøv å velge den på nytt.');
    return;
  }
  error('');
  showInfo('');
  try {
    await api('/api/client-file', { source: fileSource, content });
    showInfo('Google-filen er lagret. Velg «Logg inn» for å ta den i bruk.');
  } catch (exception) {
    error(exception.message);
  }
  await refresh();
});
refresh();
setInterval(refresh, 1200);
